# SPDX-License-Identifier: Apache-2.0

"""The value index: which columns it copies, and how it replaces its rows."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from auto_ontology.semantic import value_index
from auto_ontology.semantic.constants import LABEL_COLUMN_VALUE
from auto_ontology.semantic.embed import SemanticEmbedder
from auto_ontology.semantic.value_index import (
    ValueIndexLimits,
    build_value_rows,
    column_skip_reason,
    column_value_id,
    embed_column_values,
    read_distinct_values,
    value_index_limits,
)

_LIMITS = ValueIndexLimits(max_distinct=5, max_values=100, max_avg_length=10)


def _column(
    name: str = "country",
    *,
    table: str = "country",
    data_type: str = "text",
    pk: bool = False,
    pii: bool = False,
    processed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_name": "public",
        "table_name": table,
        "column_name": name,
        "data_type": data_type,
        "is_primary_key": pk,
        "pii_tagged": pii,
        "pii_processed": processed,
    }


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data_type", ["text", "character varying", "varchar(40)", "STRING", "USER-DEFINED"]
)
def test_string_columns_are_eligible(data_type: str) -> None:
    assert (
        column_skip_reason(_column(data_type=data_type), pii_detection_enabled=False)
        is None
    )


@pytest.mark.parametrize("data_type", ["integer", "numeric", "date", "boolean", None])
def test_other_types_are_skipped(data_type: str | None) -> None:
    reason = column_skip_reason(
        _column(data_type=data_type), pii_detection_enabled=False
    )
    assert reason == "not a string column"


def test_primary_keys_are_skipped() -> None:
    assert (
        column_skip_reason(_column(pk=True), pii_detection_enabled=False)
        == "primary key"
    )


def test_a_pii_tagged_column_is_skipped_whatever_the_detection_setting() -> None:
    for enabled in (True, False):
        assert (
            column_skip_reason(
                _column(pii=True, processed=True), pii_detection_enabled=enabled
            )
            == "tagged PII"
        )


def test_an_unclassified_column_is_skipped_only_when_detection_is_on() -> None:
    column = _column(processed=False)

    assert (
        column_skip_reason(column, pii_detection_enabled=True)
        == "not yet classified for PII"
    )
    assert column_skip_reason(column, pii_detection_enabled=False) is None
    assert (
        column_skip_reason(_column(processed=True), pii_detection_enabled=True) is None
    )


# --------------------------------------------------------------------------
# Reading values from the source
# --------------------------------------------------------------------------


class _Connector:
    dialect = "postgres"

    def qualify(self, schema: str | None, table: str) -> str:
        return f'"{schema}"."{table}"'


def _source(monkeypatch: pytest.MonkeyPatch, values: list[Any]) -> list[str]:
    """Make the source answer with *values*; returns the statements it ran."""
    ran: list[str] = []

    def _execute(_connector: Any, sql: str, **_kw: Any) -> pd.DataFrame:
        ran.append(sql)
        return pd.DataFrame({"v": values})

    monkeypatch.setattr(value_index, "execute_chat_sql", _execute)
    return ran


@pytest.mark.parametrize(
    "name",
    [
        "email",
        "Email",
        "customer_email",
        "phone",
        "mobile_number",
        "password",
        "passwd",
        "api_key",
        "access_token",
        "first_name",
        "FirstName",
        "lastname",
        "surname",
        "username",
        "address",
        "street",
        "postal_code",
        "zipcode",
        "ssn",
        "iban",
        "date_of_birth",
    ],
)
def test_a_column_named_like_personal_data_is_skipped_without_any_pii_tag(
    name: str,
) -> None:
    """Detection is off by default and nothing was tagged: the name decides."""
    reason = column_skip_reason(_column(name), pii_detection_enabled=False)

    assert reason is not None and reason.startswith("sensitive column name")


@pytest.mark.parametrize(
    "name",
    ["country", "city", "district", "title", "rating", "name", "language", "genre"],
)
def test_ordinary_label_columns_are_still_eligible(name: str) -> None:
    assert column_skip_reason(_column(name), pii_detection_enabled=False) is None


def test_the_columns_a_real_run_wrongly_indexed_are_now_skipped() -> None:
    """The index of a stock demo database held these, e-mails and a password included."""
    indexed_before = {
        ("customer", "email"),
        ("customer", "first_name"),
        ("customer", "last_name"),
        ("address", "address"),
        ("address", "phone"),
        ("address", "postal_code"),
        ("actor", "first_name"),
        ("actor", "last_name"),
        ("staff", "email"),
        ("staff", "username"),
        ("staff", "password"),
    }
    for table, name in sorted(indexed_before):
        assert (
            column_skip_reason(_column(name, table=table), pii_detection_enabled=False)
            is not None
        ), f"{table}.{name}"
    kept = [("city", "city"), ("address", "district"), ("country", "country")]
    for table, name in kept:
        assert (
            column_skip_reason(_column(name, table=table), pii_detection_enabled=False)
            is None
        )


def test_an_operator_can_re_allow_a_column_by_name_but_not_past_a_pii_tag() -> None:
    column = _column("last_name", table="customer")
    allow = frozenset({"customer.last_name"})

    assert column_skip_reason(column, pii_detection_enabled=False) is not None
    assert (
        column_skip_reason(column, pii_detection_enabled=False, include_columns=allow)
        is None
    )
    tagged = _column("last_name", table="customer", pii=True)
    assert (
        column_skip_reason(tagged, pii_detection_enabled=False, include_columns=allow)
        == "tagged PII"
    )


def test_the_include_list_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALUE_INDEX_INCLUDE_COLUMNS", " Customer.Last_Name , staff.x ")

    assert value_index.included_columns() == frozenset(
        {"customer.last_name", "staff.x"}
    )
    monkeypatch.delenv("VALUE_INDEX_INCLUDE_COLUMNS")
    assert value_index.included_columns() == frozenset()


@pytest.mark.parametrize(
    ("values", "kind"),
    [
        (["a@example.com", "b@example.org", "Paris", "Lyon"], "email"),
        (["+33 1 23 45 67 89", "01 23 45 67 89", "(555) 123-4567"], "phone"),
        (
            [
                "5d41402abc4b2a76b9719d911017c592" * 1,
                "e99a18c428cb38d5f260853678922e03",
            ],
            "hash",
        ),
        (["$2a$10$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUV"], "hash"),
        (["4111 1111 1111 1111", "5500-0000-0000-0004"], "card"),
    ],
)
def test_values_that_look_like_contact_details_or_secrets_are_refused(
    values: list[str], kind: str
) -> None:
    assert value_index.sensitive_shape(values) == f"values look like {kind} data"


@pytest.mark.parametrize(
    "values",
    [
        ["United States", "India", "France"],
        ["35200", "62709", "75001"],
        ["PG-13", "R", "NC-17"],
        ["Nancy Edwards", "Jane Peacock"],
        [],
    ],
)
def test_ordinary_values_pass_the_shape_check(values: list[str]) -> None:
    assert value_index.sensitive_shape(values) is None


def test_a_column_whose_values_look_like_emails_is_skipped_under_a_harmless_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name does not say it ("contact"); the values do."""
    monkeypatch.setattr(
        value_index,
        "execute_chat_sql",
        lambda *_a, **_k: pd.DataFrame({"contact": ["a@x.io", "b@y.io", "c@z.io"]}),
    )

    assert column_skip_reason(_column("contact"), pii_detection_enabled=False) is None
    assert read_distinct_values(_Connector(), _column("contact"), _LIMITS) is None


def test_values_are_read_with_a_bounded_distinct_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _source(monkeypatch, ["India", "Peru"])

    values = read_distinct_values(_Connector(), _column(), _LIMITS)

    assert values == ["India", "Peru"]
    assert ran == [
        'SELECT DISTINCT "country" FROM "public"."country" '
        'WHERE "country" IS NOT NULL LIMIT 6'
    ]


def test_blank_and_repeated_values_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source(monkeypatch, ["India", " India ", "", "  ", "Peru"])

    assert read_distinct_values(_Connector(), _column(), _LIMITS) == ["India", "Peru"]


def test_a_column_over_the_distinct_limit_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe asks for one more than allowed: getting it means "too many"."""
    _source(monkeypatch, [f"v{i}" for i in range(6)])

    assert read_distinct_values(_Connector(), _column(), _LIMITS) is None


def test_free_text_is_skipped_by_mean_length(monkeypatch: pytest.MonkeyPatch) -> None:
    _source(monkeypatch, ["a fairly long sentence of text", "another long sentence"])

    assert read_distinct_values(_Connector(), _column(), _LIMITS) is None


def test_a_column_that_is_not_made_of_strings_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``USER-DEFINED`` also covers non-text types; they read back as non-str."""
    _source(monkeypatch, [b"\x00\x01", b"\x02"])

    assert read_distinct_values(_Connector(), _column(), _LIMITS) is None


def test_an_unreadable_column_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_kw: Any) -> pd.DataFrame:
        raise RuntimeError("permission denied")

    monkeypatch.setattr(value_index, "execute_chat_sql", _boom)

    assert read_distinct_values(_Connector(), _column(), _LIMITS) is None


def test_limits_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert value_index_limits() == ValueIndexLimits(2000, 20000, 40)

    monkeypatch.setenv("VALUE_INDEX_MAX_DISTINCT", "50")
    monkeypatch.setenv("VALUE_INDEX_MAX_VALUES", "nonsense")

    limits = value_index_limits()
    assert limits.max_distinct == 50
    assert limits.max_values == 20000


# --------------------------------------------------------------------------
# Rows and ids
# --------------------------------------------------------------------------


def test_the_id_of_a_value_is_deterministic_and_specific() -> None:
    first = column_value_id("pagila", "public", "country", "country", "India")

    assert first == column_value_id("pagila", "public", "country", "country", "India")
    others = {
        column_value_id("other", "public", "country", "country", "India"),
        column_value_id("pagila", "public", "country", "country", "Peru"),
        column_value_id("pagila", "public", "city", "country", "India"),
        column_value_id("pagila", "public", "country", "name", "India"),
    }
    assert first not in others
    assert len(others) == 4


def test_rows_carry_the_value_and_where_it_lives() -> None:
    (row,) = build_value_rows("pagila", _column(), ["India"])

    meta = row["metadata"]
    assert row["text"] == "India"
    assert meta["label"] == LABEL_COLUMN_VALUE
    assert meta["name"] == "India"
    assert meta["database_name"] == "pagila"
    assert meta["schema_name"] == "public"
    assert meta["table_name"] == "country"
    assert meta["source_column"] == "country"
    assert meta["id"] == column_value_id(
        "pagila", "public", "country", "country", "India"
    )
    # Retrieval reads what the row builders also put under content_metadata.
    assert meta["content_metadata"]["table_name"] == "country"


# --------------------------------------------------------------------------
# Rebuilding
# --------------------------------------------------------------------------


class _Vdb:
    """Records, in order, what the build asks of the vector store."""

    def __init__(
        self, existing: set[str] | None = None, stored: list[dict] | None = None
    ) -> None:
        self.existing = existing or set()
        self.stored = stored or []
        self.events: list[tuple[str, Any]] = []

    def values_by_database(self, database_name: str, label: str) -> list[dict]:
        return self.stored

    def ids_by_database(self, database_name: str, labels: list[str]) -> set[str]:
        self.events.append(("ids", (database_name, labels)))
        return set(self.existing)

    def delete_by_ids(self, node_ids: list[str], database_name: str) -> int:
        self.events.append(("delete", (sorted(node_ids), database_name)))
        return len(node_ids)


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch):
    """A catalog and source to build from; returns a runner for the build."""

    def _run(
        columns: list[dict[str, Any]],
        source: dict[str, list[str]],
        *,
        vdb: _Vdb | None = None,
        unembeddable: set[str] = frozenset(),  # type: ignore[assignment]
        limits: ValueIndexLimits = _LIMITS,
        pii_detection: bool = False,
    ) -> tuple[Any, _Vdb, list[str]]:
        vdb = vdb or _Vdb()
        read: list[str] = []

        monkeypatch.setattr(
            value_index, "fetch_value_index_candidates", lambda _db: columns
        )
        monkeypatch.setattr(
            value_index, "is_pii_detection_enabled", lambda: pii_detection
        )
        monkeypatch.setattr(value_index, "value_index_limits", lambda: limits)

        def _read(_conn: Any, column: dict[str, Any], _limits: Any) -> list[str] | None:
            read.append(column["column_name"])
            return source.get(column["column_name"])

        monkeypatch.setattr(value_index, "read_distinct_values", _read)

        def _embed(rows: list[dict], _params: Any) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        **r,
                        "metadata": {
                            **r["metadata"],
                            "embedding": [] if r["text"] in unembeddable else [0.1],
                        },
                    }
                    for r in rows
                ]
            )

        monkeypatch.setattr(value_index, "batch_embed", _embed)

        def _write(store: _Vdb, rows: list[dict]) -> int:
            store.events.append(("write", sorted(r["text"] for r in rows)))
            store.events.append(
                ("aliases", {r["text"]: r["metadata"].get("aliases") for r in rows})
            )
            return len(rows)

        monkeypatch.setattr(value_index, "write_embedded_rows", _write)
        embedder = SemanticEmbedder("pagila", object(), vdb)  # type: ignore[arg-type]
        return embed_column_values(embedder, _Connector()), vdb, read

    return _run


def test_a_build_reports_what_it_indexed_and_skipped(build) -> None:
    result, _vdb, _read = build(
        [_column("country"), _column("country_id", data_type="integer")],
        {"country": ["India", "Peru"]},
    )

    assert (result.expected, result.written) == (2, 2)
    assert (result.columns_indexed, result.columns_skipped) == (1, 1)


def test_excluded_columns_are_never_read_from_the_source(build) -> None:
    """The point of the rules: what is excluded never leaves the database."""
    columns = [
        _column("country"),
        _column("email", pii=True),
        _column("customer_id", pk=True),
        _column("amount", data_type="numeric"),
    ]

    _result, _vdb, read = build(
        columns, {"country": ["India"], "email": ["a@b.c"], "customer_id": ["1"]}
    )

    assert read == ["country"]


def test_rows_are_deleted_before_they_are_written(build) -> None:
    _result, vdb, _read = build([_column("country")], {"country": ["India", "Peru"]})

    names = [name for name, _ in vdb.events]
    assert names.index("delete") < names.index("write")
    deleted = next(arg for name, arg in vdb.events if name == "delete")
    assert deleted[0] == sorted(
        column_value_id("pagila", "public", "country", "country", v)
        for v in ("India", "Peru")
    )


def test_rows_no_longer_wanted_are_deleted(build) -> None:
    """A column since tagged PII must lose the values it already has indexed."""
    gone = column_value_id("pagila", "public", "customer", "last_name", "Smith")
    kept = column_value_id("pagila", "public", "country", "country", "India")

    _result, vdb, _read = build(
        [_column("country"), _column("last_name", table="customer", pii=True)],
        {"country": ["India"]},
        vdb=_Vdb({gone, kept}),
    )

    deletes = [arg[0] for name, arg in vdb.events if name == "delete"]
    assert [gone] in deletes


def test_a_failing_endpoint_removes_nothing_that_still_works(build) -> None:
    result, vdb, _read = build(
        [_column("country")],
        {"country": ["India", "Peru"]},
        unembeddable={"Peru"},
    )

    assert (result.expected, result.written) == (2, 1)
    peru = column_value_id("pagila", "public", "country", "country", "Peru")
    deleted = [i for name, arg in vdb.events if name == "delete" for i in arg[0]]
    assert peru not in deleted
    assert ("write", ["India"]) in vdb.events


def test_a_column_that_would_overflow_the_budget_is_skipped_whole(build) -> None:
    limits = ValueIndexLimits(max_distinct=5, max_values=3, max_avg_length=10)

    result, _vdb, _read = build(
        [_column("a"), _column("b")],
        {"a": ["x1", "x2"], "b": ["y1", "y2"]},
        limits=limits,
    )

    assert (result.expected, result.columns_indexed, result.columns_skipped) == (
        2,
        1,
        1,
    )


def test_an_unclassified_column_is_left_out_when_detection_is_on(build) -> None:
    result, _vdb, read = build(
        [_column("country", processed=True), _column("city")],
        {"country": ["India"], "city": ["Delhi"]},
        pii_detection=True,
    )

    assert read == ["country"]
    assert result.expected == 1


def test_a_build_forgets_what_lookups_cached(
    build, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleared: list[str | None] = []
    from auto_ontology.retrieval.text_to_sql import value_linking

    monkeypatch.setattr(value_linking, "clear_value_cache", cleared.append)

    build([_column("country")], {"country": ["India"]})

    assert cleared == ["pagila"]


def test_aliases_users_confirmed_survive_a_rebuild(build) -> None:
    """They live on the rows a rebuild replaces, so they are carried over."""
    stored = [
        {
            "value": "United States",
            "schema_name": "public",
            "table_name": "country",
            "column_name": "country",
            "aliases": ["the states"],
        }
    ]

    _result, vdb, _read = build(
        [_column("country")],
        {"country": ["United States", "Peru"]},
        vdb=_Vdb(stored=stored),
    )

    assert ("aliases", {"United States": ["the states"], "Peru": None}) in vdb.events
