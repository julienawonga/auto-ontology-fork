# SPDX-License-Identifier: Apache-2.0

"""An opt-in index of the values stored in a database's text columns.

The chat answers from SQL a model writes, and a model writes the literal the
user typed: "USA" where the data stores "United States", "Indai" where it
stores "India". The query returns nothing and says so with confidence. The
schema cannot help, because a value is data, and a column too large to list in
a prompt is out of reach of the live-database probes.

This module copies the distinct values of the *small* text columns into the
semantic vector index, one row per value, so that a phrase from a question can
be looked up (see ``auto_ontology.retrieval.text_to_sql.value_linking``). It is
deliberately narrow about what it copies, because it moves data out of the
source database:

* only string-typed columns that are neither primary keys nor tagged ``PII``,
  whose name does not say personal data or a secret (e-mail, phone, password,
  address, first or last name, ...) and whose values do not look like contact
  details or secrets (the name rule can be lifted per column with
  ``VALUE_INDEX_INCLUDE_COLUMNS``, the value check never);
* only columns with at most ``VALUE_INDEX_MAX_DISTINCT`` distinct values, which
  also keeps most high-cardinality columns out;
* not free text: a column whose values average more than
  ``VALUE_INDEX_MAX_AVG_LENGTH`` characters is skipped;
* no NULL or empty values, and a per-database budget of
  ``VALUE_INDEX_MAX_VALUES`` rows that bounds embedding calls and storage.

It never runs by itself: ingestion and compilation do not call it. A person
asks for it, per database, through ``POST /semantic-compilation/value-index``.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

from auto_ontology.dal.datasources import fetch_value_index_candidates
from auto_ontology.infra.feature_flags import is_pii_detection_enabled
from auto_ontology.retrieval.text_to_sql.chat_sql import (
    TRUNCATED_AT_ATTR,
    execute_chat_sql,
)
from auto_ontology.semantic.constants import LABEL_COLUMN_VALUE
from auto_ontology.semantic.embed import SemanticEmbedder
from auto_ontology.utils.embedding import batch_embed
from auto_ontology.utils.sql_identifiers import quoted_identifier
from auto_ontology.vdb.records import write_embedded_rows

logger = logging.getLogger(__name__)

# Types the source reports for text: char/varchar/text/string/citext, and enums
# (PostgreSQL reports its own enum types as ``USER-DEFINED``, which also covers
# non-text types -- those are caught by checking that the values read back are
# strings).
_STRING_TYPE = re.compile(r"char|text|string|citext|enum|user-defined", re.IGNORECASE)

# Longer than this is a sentence, not a label worth matching against.
_MAX_VALUE_LENGTH = 100

# Column names that say what the column holds. Automatic PII classification is
# off by default and a hand-set tag is rarely there, so a column that is merely
# "not tagged" is NOT known to be safe: on the first real run of this index a
# stock demo database had customer e-mails, phone numbers, names and a staff
# password column indexed (and sent to the embedding endpoint). The name is the
# cheapest signal that does not depend on any setting. Matched on the name with
# its separators removed, so ``first_name``, ``FirstName`` and ``firstname`` are
# one thing. Deliberately conservative: a false hit only costs a column that
# can be re-allowed by name (``VALUE_INDEX_INCLUDE_COLUMNS``).
_SENSITIVE_NAME_PARTS = (
    "password",
    "passwd",
    "passcode",
    "pwd",
    "secret",
    "token",
    "apikey",
    "credential",
    "ssn",
    "socialsecurity",
    "iban",
    "creditcard",
    "cardnumber",
    "cvv",
    "email",
    "phone",
    "mobile",
    "fax",
    "address",
    "street",
    "postal",
    "postcode",
    "zipcode",
    "birth",
    "dob",
    "firstname",
    "lastname",
    "surname",
    "fullname",
    "username",
    "login",
    "ipaddress",
)

# The shape of what a column holds, checked on its values. This catches what the
# name does not say (a column called ``contact`` full of e-mails) and is never
# switched off by the include list: a column whose values look like secrets or
# contact details is not indexed whatever it is called.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,25}$")
_HASH = re.compile(r"^(?:[A-Fa-f0-9]{24,}|[A-Za-z0-9+/=_-]{32,}|\$[0-9a-z]{1,3}\$\S+)$")
# Share of a column's values that must look like the thing for it to be skipped.
_SHAPE_THRESHOLDS = {"email": 0.1, "phone": 0.3, "hash": 0.3, "card": 0.1}

# A name for the id derivation, so the same value always gets the same id.
_ID_NAMESPACE = uuid.UUID("6f1c9a4e-3b7d-5c2a-9e08-1d4b7a3c5f60")

# Seconds. A DISTINCT over a large column can be slow; the probe is capped so
# one bad column cannot hold the request.
_PROBE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ValueIndexLimits:
    """What a column must stay under to be indexed. All are environment knobs."""

    #: A column with more distinct values than this is skipped entirely.
    max_distinct: int
    #: Rows the whole database may hold. A column that would overflow it is
    #: skipped whole rather than cut, so a column is indexed completely or not
    #: at all.
    max_values: int
    #: Mean value length above which a column is treated as free text.
    max_avg_length: int


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def value_index_limits() -> ValueIndexLimits:
    return ValueIndexLimits(
        max_distinct=_int_env("VALUE_INDEX_MAX_DISTINCT", 2000),
        max_values=_int_env("VALUE_INDEX_MAX_VALUES", 20000),
        max_avg_length=_int_env("VALUE_INDEX_MAX_AVG_LENGTH", 40),
    )


@dataclass(frozen=True)
class ValueIndexRebuild:
    """Outcome of :func:`embed_column_values`.

    ``written < expected`` is a partial build: the embedding endpoint did not
    return a vector for every row. ``columns_skipped`` counts the columns that
    were not eligible or exceeded a limit, which is the normal case for most of
    a schema.
    """

    expected: int
    written: int
    columns_indexed: int
    columns_skipped: int


def included_columns() -> frozenset[str]:
    """``table.column`` names an operator re-allowed, from ``VALUE_INDEX_INCLUDE_COLUMNS``.

    Comma separated, case-insensitive. It lifts the NAME rule only, never the
    PII tag and never the shape check on the values.
    """
    raw = os.environ.get("VALUE_INDEX_INCLUDE_COLUMNS", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _sensitive_name(column_name: str) -> str | None:
    """The part of *column_name* that marks it as sensitive, if any."""
    flat = re.sub(r"[^a-z0-9]", "", str(column_name).lower())
    return next((part for part in _SENSITIVE_NAME_PARTS if part in flat), None)


def column_skip_reason(
    column: dict[str, Any],
    *,
    pii_detection_enabled: bool,
    include_columns: frozenset[str] = frozenset(),
) -> str | None:
    """Why *column* must not be indexed, or ``None`` when it may be.

    A column that is not tagged ``PII`` is not thereby known to be clean:
    automatic classification is off by default and a tag is rarely set by hand.
    So the column's name is checked as well (see ``_SENSITIVE_NAME_PARTS``), and
    when classification is on, an unclassified column is left out rather than
    assumed clean. The values themselves are checked once read, by
    :func:`sensitive_shape`.
    """
    if not _STRING_TYPE.search(str(column.get("data_type") or "")):
        return "not a string column"
    if column.get("is_primary_key"):
        return "primary key"
    if column.get("pii_tagged"):
        return "tagged PII"
    if pii_detection_enabled and not column.get("pii_processed"):
        return "not yet classified for PII"
    name = str(column.get("column_name") or "")
    qualified = f"{column.get('table_name') or ''}.{name}".lower()
    if (part := _sensitive_name(name)) and qualified not in include_columns:
        return f"sensitive column name ({part})"
    return None


def sensitive_shape(values: list[str]) -> str | None:
    """Why *values* look like contact details or secrets, or ``None``.

    A share of e-mail addresses, phone numbers, card numbers or hashes and
    tokens, whatever the column is called. Checked on the distinct values.
    """
    if not values:
        return None
    counts = {"email": 0, "phone": 0, "hash": 0, "card": 0}
    for value in values:
        digits = sum(ch.isdigit() for ch in value)
        # A card number also fits the phone pattern: name it as a card.
        if _EMAIL.match(value):
            counts["email"] += 1
        elif 13 <= digits <= 19 and re.fullmatch(r"[\d \-]+", value):
            counts["card"] += 1
        elif _PHONE.match(value) and digits >= 7:
            counts["phone"] += 1
        if _HASH.match(value):
            counts["hash"] += 1
    for kind, threshold in _SHAPE_THRESHOLDS.items():
        if counts[kind] / len(values) >= threshold:
            return f"values look like {kind} data"
    return None


def read_distinct_values(
    connector: Any, column: dict[str, Any], limits: ValueIndexLimits
) -> list[str] | None:
    """The distinct non-empty values of *column*, or ``None`` if it is unfit.

    ``None`` means: more distinct values than allowed, free text, not made of
    strings, or unreadable. Reads at most ``max_distinct + 1`` values, so a
    huge column costs one bounded scan, not a full one. Goes through
    :func:`execute_chat_sql`, so the statement is checked to be a read and runs
    in the database's read-only mode where it has one.
    """
    dialect = getattr(connector, "dialect", None)
    quoted = quoted_identifier(column["column_name"], dialect)
    table = connector.qualify(column.get("schema_name"), column["table_name"])
    sql = (
        f"SELECT DISTINCT {quoted} FROM {table} "
        f"WHERE {quoted} IS NOT NULL LIMIT {limits.max_distinct + 1}"
    )
    try:
        frame = execute_chat_sql(
            connector, sql, kind="value index", timeout_s=_PROBE_TIMEOUT_S
        )
    except Exception:
        logger.warning(
            "Value index: could not read %s.%s",
            column["table_name"],
            column["column_name"],
            exc_info=True,
        )
        return None
    if frame is None or frame.empty or TRUNCATED_AT_ATTR in frame.attrs:
        return None
    raw = frame.iloc[:, 0].tolist()
    if len(raw) > limits.max_distinct or not all(isinstance(v, str) for v in raw):
        return None
    values = list(dict.fromkeys(v.strip() for v in raw if v.strip()))
    values = [v for v in values if len(v) <= _MAX_VALUE_LENGTH]
    if not values:
        return None
    if sum(len(v) for v in values) / len(values) > limits.max_avg_length:
        return None
    if reason := sensitive_shape(values):
        logger.info(
            "Value index: skipped %s.%s (%s)",
            column["table_name"],
            column["column_name"],
            reason,
        )
        return None
    return values


def column_value_id(
    database_name: str,
    schema_name: str | None,
    table_name: str,
    column_name: str,
    value: str,
) -> str:
    """Deterministic id of one value row, so a rebuild replaces it in place."""
    key = "\x1f".join(
        [database_name, schema_name or "", table_name, column_name, value]
    )
    return str(uuid.uuid5(_ID_NAMESPACE, key))


def build_value_rows(
    database_name: str, column: dict[str, Any], values: list[str]
) -> list[dict[str, Any]]:
    """Embeddable rows, one per value, in the shape ``write_embedded_rows`` takes.

    The embedded text is the bare value: what a question's phrase is compared
    with is the value itself, and a prefix such as the column name would pull
    every value of a column toward each other.
    """
    schema_name = column.get("schema_name")
    path = f"value:{schema_name or ''}.{column['table_name']}.{column['column_name']}"
    rows: list[dict[str, Any]] = []
    for value in values:
        fields: dict[str, Any] = {
            "label": LABEL_COLUMN_VALUE,
            "name": value,
            "database_name": database_name,
            "table_name": column["table_name"],
            "source_column": column["column_name"],
            "source_path": path,
            "id": column_value_id(
                database_name,
                schema_name,
                column["table_name"],
                column["column_name"],
                value,
            ),
        }
        if schema_name:
            fields["schema_name"] = schema_name
        rows.append(
            {
                "text": value,
                "path": path,
                "page_number": -1,
                "metadata": {**fields, "content_metadata": dict(fields)},
            }
        )
    return rows


def embed_column_values(
    embedder: SemanticEmbedder, connector: Any
) -> ValueIndexRebuild:
    """Rebuild the value rows of the embedder's database from the source.

    Safe to repeat. Each value is stored under a deterministic id and the rows
    about to be rewritten are deleted first, because the vector store only
    appends. Rows whose id is no longer wanted -- a value that left the data, a
    column since tagged ``PII`` or grown past a limit -- are deleted too, so
    tightening the rules removes what they now exclude. Only rows that came
    back with an embedding replace anything, so a failing endpoint removes
    nothing that still works.
    """
    from auto_ontology.retrieval.text_to_sql import value_linking

    database_name = embedder.database_name
    limits = value_index_limits()
    pii_detection = is_pii_detection_enabled()
    include = included_columns()

    rows: list[dict[str, Any]] = []
    indexed = skipped = 0
    for column in fetch_value_index_candidates(database_name):
        label = f"{column['table_name']}.{column['column_name']}"
        reason = column_skip_reason(
            column, pii_detection_enabled=pii_detection, include_columns=include
        )
        values = None if reason else read_distinct_values(connector, column, limits)
        if values is None or len(rows) + len(values) > limits.max_values:
            skipped += 1
            logger.info("Value index: skipped %s (%s)", label, reason or "limits")
            continue
        rows.extend(build_value_rows(database_name, column, values))
        indexed += 1

    # A rebuild replaces rows, and the aliases users confirmed live on them.
    known = {
        column_value_id(
            database_name,
            r["schema_name"],
            r["table_name"],
            r["column_name"],
            r["value"],
        ): r["aliases"]
        for r in embedder.vdb.values_by_database(database_name, LABEL_COLUMN_VALUE)
        if r.get("aliases")
    }
    for row in rows:
        if aliases := known.get(row["metadata"]["id"]):
            row["metadata"]["aliases"] = aliases
            row["metadata"]["content_metadata"]["aliases"] = aliases

    wanted_ids = {row["metadata"]["id"] for row in rows}
    stale = (
        embedder.vdb.ids_by_database(database_name, [LABEL_COLUMN_VALUE]) - wanted_ids
    )
    if stale:
        embedder.vdb.delete_by_ids(sorted(stale), database_name)

    written = 0
    if rows:
        embedded = batch_embed(rows, embedder.embed_params)
        with_embeddings = (
            []
            if embedded is None or embedded.empty
            else [
                r
                for r in embedded.to_dict(orient="records")
                if (r.get("metadata") or {}).get("embedding")
            ]
        )
        if with_embeddings:
            embedder.vdb.delete_by_ids(
                [r["metadata"]["id"] for r in with_embeddings], database_name
            )
            written = write_embedded_rows(embedder.vdb, with_embeddings)
    log = logger.info if written == len(rows) else logger.warning
    log(
        "Value index: %d/%d row(s) written for %r (%d column(s) indexed, %d skipped)",
        written,
        len(rows),
        database_name,
        indexed,
        skipped,
    )
    # Lookups cache what they read; drop it so the next question sees this build.
    value_linking.clear_value_cache(database_name)
    return ValueIndexRebuild(
        expected=len(rows),
        written=written,
        columns_indexed=indexed,
        columns_skipped=skipped,
    )
