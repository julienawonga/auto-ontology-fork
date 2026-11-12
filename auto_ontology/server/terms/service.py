# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Term read + update orchestration."""

from __future__ import annotations

import logging
import re
from typing import Any

from auto_ontology.dal import attributes as attributes_dal
from auto_ontology.dal import sql_attributes as sql_attr_dal
from auto_ontology.dal import terms as terms_dal
from auto_ontology.dal.users import resolve_accessible_catalog_ids
from auto_ontology.semantic.embed import build_semantic_embedder
from auto_ontology.server.datasources import service as datasources_service
from auto_ontology.utils import (
    get_embed_params,
    sample_values_edit_error,
    stringify_sample_values,
)
from auto_ontology.utils.embedding import embed_docs_into_vdb
from auto_ontology.vdb import get_semantic_vdb

logger = logging.getLogger(__name__)


def list_terms_page(
    *,
    zone_ids: list[str] | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Assemble one page of the Terms list with its per-card count breakdowns.

    Resolving *zone_ids* to accessible catalog ids costs several store round
    trips, so it happens once here and is threaded through the reads that
    accept it (see ``resolve_accessible_catalog_ids``).

    ``total`` counts every term matching *search* and *zone_ids*, not the ones
    on this page, so the caller can tell whether further pages exist.

    The three count lists are per-term breakdowns keyed by ``term_id``. When a
    *limit* is given they cover only the terms on this page — the whole point
    of paging is not to describe the rest of the glossary — so a caller
    holding pages must merge them rather than replace what it already has.
    Called without a *limit* they still cover every visible term.
    """
    data_ids_by_zone = resolve_accessible_catalog_ids(zone_ids)
    terms = terms_dal.fetch_all_terms(
        zone_ids=zone_ids,
        search=search,
        data_ids_by_zone=data_ids_by_zone,
        skip=skip,
        limit=limit,
    )
    # `terms` only holds every matching term when neither paging argument was
    # given — a `skip` alone (no `limit`) still returns a partial read, so it
    # needs the same separate count query and the same page-scoped id list.
    paged = bool(skip) or limit is not None
    page_term_ids = [term["id"] for term in terms] if paged else None
    total = (
        terms_dal.count_terms(
            zone_ids=zone_ids, search=search, data_ids_by_zone=data_ids_by_zone
        )
        if paged
        else len(terms)
    )
    return {
        "terms": terms,
        "total": total,
        "column_attribute_counts": terms_dal.fetch_column_attribute_counts(
            zone_ids=zone_ids,
            data_ids_by_zone=data_ids_by_zone,
            term_ids=page_term_ids,
        ),
        "sql_attribute_counts": sql_attr_dal.fetch_sql_attribute_counts(
            zone_ids=zone_ids,
            data_ids_by_zone=data_ids_by_zone,
            term_ids=page_term_ids,
        ),
        "related_counts": terms_dal.fetch_related_terms_counts(
            zone_ids=zone_ids,
            term_ids=page_term_ids,
            data_ids_by_zone=data_ids_by_zone,
        ),
    }


def update_column_attribute(
    term_id: str,
    attr_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    sample_values: list[str] | None = None,
    certified: bool | None = None,
    modified_by: str | None = None,
    touch: bool = False,
) -> dict[str, Any] | None:
    """Update ColumnAttribute metadata and refresh related VDB rows.

    ``sample_values`` live on the owning Column. When provided, this updates
    that Column (same path as catalog column edit), re-embeds the Column in
    the data VDB, then re-embeds this ColumnAttribute in the semantic VDB.

    The certification flag carries no embedding content, so a
    certification-only update skips the VDB refresh entirely.

    Raises ``ValueError`` when the owning Column's declared SQL type does not
    accept hand-written samples.
    """
    if sample_values is not None:
        # Refused ahead of the metadata write below, so a rejected patch does
        # not leave a renamed attribute behind. A missing attribute or Column
        # is left to that write, which answers 404 rather than a type
        # complaint; the Column write itself re-checks the type.
        owning_column = attributes_dal.fetch_column_attribute_column(attr_id)
        if owning_column is not None:
            error = sample_values_edit_error(owning_column.get("data_type"))
            if error is not None:
                raise ValueError(error)

    row = attributes_dal.update_column_attribute(
        attr_id,
        term_id,
        name=name,
        description=description,
        certified=certified,
        modified_by=modified_by,
        touch=touch,
    )
    if row is None:
        return None

    content_changed = (
        name is not None or description is not None or sample_values is not None
    )
    if not content_changed:
        row["sample_values"] = stringify_sample_values(row.get("sample_values"))
        return row

    if sample_values is not None:
        column_id = row.get("column_id")
        if not column_id:
            raise ValueError(
                "ColumnAttribute has no owning Column; sample values cannot be updated"
            )
        # Reuses catalog Column patch: writes Column.sample_values and
        # refreshes the Column row in the data VDB. It also best-effort
        # refreshes semantic ColumnAttribute rows; we still re-embed this
        # attribute below so Term synonyms stay in the semantic text.
        datasources_service.update_node_properties(
            column_id,
            {"sample_values": sample_values},
        )
        row["sample_values"] = sample_values
    else:
        row["sample_values"] = stringify_sample_values(row.get("sample_values"))

    embedder = build_semantic_embedder(row.get("database_name") or "", reset=False)
    if embedder is not None:
        embedder.vdb.delete_by_id(attr_id)
        attr = {
            "id": row["id"],
            "name": row["name"],
            "description": row.get("description"),
            "term_name": row.get("term_name"),
            "source_column": row.get("source_column"),
            "sample_values": row.get("sample_values"),
            "is_unique": row.get("is_unique"),
            "table_id": row.get("table_id"),
            "table_name": row.get("table_name"),
            "schema_name": row.get("schema_name"),
        }
        # Pass Term synonyms only — omit Term name so we re-embed just this attr row.
        term_ctx = {"synonyms": row.get("term_synonyms") or []}
        embedder.embed_term(term_ctx, [attr])

    return row


def refresh_term_embeddings(term_id: str, *, refresh_dependent_attrs: bool) -> bool:
    """Best-effort semantic VDB refresh for one Term, a single-row upsert.

    The Term's row is deleted by id and written again; nothing else in the
    index is touched. Returns whether at least one row was written (``False``
    when embedding is disabled or the endpoint returned nothing).
    """
    term, column_attrs = terms_dal.fetch_term_and_column_attributes_for_embedding(
        term_id
    )
    if term is None:
        return False

    embedder = build_semantic_embedder(term.get("database_name") or "", reset=False)
    if embedder is None:
        return False

    vdb = embedder.vdb
    vdb.delete_by_id(term_id)
    if refresh_dependent_attrs:
        for attr in column_attrs:
            attr_id = attr.get("id")
            if attr_id:
                vdb.delete_by_id(attr_id)

    written = embedder.embed_term(term, column_attrs if refresh_dependent_attrs else [])

    if refresh_dependent_attrs:
        embed_params = get_embed_params()
        sql_attrs = sql_attr_dal.fetch_sql_attributes_by_term_id(term_id)
        for sql_attr in sql_attrs:
            attr_id = sql_attr.get("id")
            if not attr_id:
                continue
            vdb.delete_by_id(attr_id)
            embed_docs_into_vdb(
                sql_attr_dal.fetch_sql_attribute_docs(attr_id),
                embed_params,
                vdb,
                term.get("database_name"),
            )

    return written > 0


# A column or table name shorter than this is too common a word to link a Term by.
_MIN_LINK_NAME_LEN = 4


class UnknownDatabase(LookupError):
    """The database named in a create request is not in the catalog."""

    def __init__(self, database: str) -> None:
        self.database = database
        super().__init__(f"Unknown database {database!r}")


def link_tables_for_text(
    text: str, tables: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """Tables whose name or columns *text* mentions, as ``(ids, how)``.

    A Term belongs to a database only through the tables that represent it, and
    the SQL path adds a retrieved Term's tables to the prompt, so attaching every
    table would flood it. Names are matched as whole words, case-insensitively,
    and only when at least four characters long. When nothing matches, falls
    back to the first table by name so the Term is still scoped to its database;
    ``how`` is ``"matched"`` or ``"fallback"`` so the caller can tell the two apart.
    """
    haystack = text.lower()

    def mentioned(name: str) -> bool:
        return len(name) >= _MIN_LINK_NAME_LEN and bool(
            re.search(
                rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])", haystack
            )
        )

    hits = [
        table["id"]
        for table in tables
        if mentioned(table["name"]) or any(mentioned(c) for c in table["columns"])
    ]
    if hits:
        return hits, "matched"
    if tables:
        return [min(tables, key=lambda t: (t["name"], t["id"]))["id"]], "fallback"
    return [], "fallback"


def create_term(
    *,
    name: str,
    description: str | None,
    synonyms: list[str] | None,
    database: str | None,
    table_ids: list[str] | None,
    created_by: str | None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Create a Term, link its tables and embed it, in that order.

    Raises ``UnknownDatabase``, ``terms_dal.UnknownTableIds``,
    ``terms_dal.TermSpansDatabases`` and ``terms_dal.TermNameTaken``. The
    returned dict carries ``table_ids``, ``linked_by`` (``explicit``,
    ``matched`` or ``fallback``) and ``embedded``.

    The embedding is best effort: the row is already committed, so a failing
    embedding endpoint leaves ``embedded`` false (and a WARNING) rather than
    failing a create that succeeded. ``POST /semantic-compilation/reindex``
    repairs the index.
    """
    if table_ids:
        chosen = list(dict.fromkeys(table_ids))
        db = terms_dal.database_for_tables(chosen)
        if database is not None:
            named = terms_dal.resolve_database(database)
            if named is None:
                raise UnknownDatabase(database)
            if named["id"] != db["id"]:
                raise terms_dal.TermSpansDatabases([named["name"], db["name"]])
        linked_by = "explicit"
    else:
        if database is None:
            raise ValueError("Give a database or table_ids")
        db = terms_dal.resolve_database(database)
        if db is None:
            raise UnknownDatabase(database)
        candidates = terms_dal.fetch_database_link_candidates(db["id"])
        chosen, linked_by = link_tables_for_text(
            f"{name} {description or ''}", candidates
        )
        if not chosen:
            raise ValueError(f"Database {db['name']!r} has no tables to link a Term to")

    created = terms_dal.create_term(
        name=name,
        description=description,
        synonyms=synonyms,
        table_ids=chosen,
        created_by=created_by,
        owner_id=owner_id,
    )
    created["linked_by"] = linked_by
    created["database_name"] = db["name"]
    try:
        created["embedded"] = refresh_term_embeddings(
            created["id"], refresh_dependent_attrs=False
        )
    except Exception:
        logger.warning(
            "Term %r was created but not embedded", created["id"], exc_info=True
        )
        created["embedded"] = False
    return created


def delete_term(term_id: str) -> dict[str, Any] | None:
    """Delete a Term and every semantic-index row it owns. ``None`` if missing.

    The vector rows (the Term's and its ColumnAttributes') go first, then the
    database rows. If the index delete fails nothing was changed and the caller
    may retry; the opposite order would leave embeddings of a Term that no
    longer exists, which retrieval would keep returning. ``delete_by_id`` is not
    scoped to a database, so a Term represented in several databases is cleared
    from all of them.

    Raises ``terms_dal.TermHasSqlAttributes`` (before touching anything).
    """
    owned = terms_dal.fetch_term_vector_ids(term_id)
    if owned is None:
        return None
    if owned["sql_attribute_names"]:
        raise terms_dal.TermHasSqlAttributes(owned["sql_attribute_names"])

    vdb = get_semantic_vdb()
    vector_rows = 0
    for node_id in [term_id, *owned["attribute_ids"]]:
        vector_rows += vdb.delete_by_id(node_id)

    terms_dal.delete_term(term_id)
    owned["vector_rows_deleted"] = vector_rows
    return owned
