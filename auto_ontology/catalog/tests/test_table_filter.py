# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regex allow/deny filtering of relations at catalog-extraction time."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from auto_ontology.catalog.extract import create_dataframe
from auto_ontology.catalog.table_filter import (
    InvalidTableFilterError,
    TableFilter,
    from_connection,
)


def _connector(tables: list[dict], columns: list[dict], **kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        get_tables=lambda: pd.DataFrame(tables),
        get_columns=lambda: pd.DataFrame(columns),
        get_views=lambda: pd.DataFrame(),
        get_queries=lambda: pd.DataFrame(),
        get_pks=lambda: pd.DataFrame(),
        get_fks=lambda: pd.DataFrame(),
        **kwargs,
    )


def test_deny_drops_matching_relations() -> None:
    f = from_connection({"table_deny_regex": "^tmp_"})
    assert f.keep("orders") is True
    assert f.keep("tmp_orders") is False


def test_allow_keeps_only_matching_relations() -> None:
    f = from_connection({"table_allow_regex": "^fact_"})
    assert f.keep("fact_sales") is True
    assert f.keep("dim_date") is False


def test_deny_wins_over_allow() -> None:
    """So "this family, except these" is expressible."""
    f = from_connection(
        {"table_allow_regex": "^fact_", "table_deny_regex": "_deprecated$"}
    )
    assert f.keep("fact_sales") is True
    assert f.keep("fact_sales_deprecated") is False


def test_patterns_are_unanchored_and_case_sensitive() -> None:
    f = from_connection({"table_deny_regex": "stage"})
    assert f.keep("pg_perfbot_flat_all_stage") is False  # substring match
    assert f.keep("pg_perfbot_flat_all_STAGE") is True  # case-sensitive


def test_blank_and_missing_patterns_are_inactive() -> None:
    assert from_connection({}).active is False
    assert from_connection({"table_allow_regex": "   "}).active is False
    assert from_connection(None).active is False


def test_invalid_regex_raises_rather_than_being_ignored() -> None:
    """Silently dropping it would ingest everything the operator excluded."""
    with pytest.raises(InvalidTableFilterError) as exc:
        from_connection({"table_deny_regex": "([unclosed"})
    assert "Table denylist" in str(exc.value)


def test_create_dataframe_applies_the_connectors_filter() -> None:
    tables = [
        {"table_schema": "public", "table_name": "orders"},
        {"table_schema": "public", "table_name": "tmp_orders"},
    ]
    columns = [
        {"table_schema": "public", "table_name": "orders", "column_name": "id"},
        {"table_schema": "public", "table_name": "tmp_orders", "column_name": "id"},
    ]
    connector = _connector(
        tables,
        columns,
        table_filter=TableFilter(
            deny=from_connection({"table_deny_regex": "^tmp_"}).deny
        ),
    )

    out_tables, out_columns, *_ = create_dataframe(connector)

    assert list(out_tables["table_name"]) == ["orders"]
    # Columns must be filtered in lockstep, or the coverage check below would
    # see a described relation that is no longer listed.
    assert list(out_columns["table_name"]) == ["orders"]


def test_create_dataframe_without_a_filter_is_unchanged() -> None:
    tables = [{"table_schema": "public", "table_name": "orders"}]
    columns = [{"table_schema": "public", "table_name": "orders", "column_name": "id"}]

    out_tables, out_columns, *_ = create_dataframe(_connector(tables, columns))

    assert list(out_tables["table_name"]) == ["orders"]
    assert list(out_columns["table_name"]) == ["orders"]


def test_filtering_does_not_trip_the_column_coverage_guard() -> None:
    """Dropping a table whose columns remain would raise; it must not."""
    tables = [
        {"table_schema": "s", "table_name": "keep"},
        {"table_schema": "s", "table_name": "drop_me"},
    ]
    columns = [
        {"table_schema": "s", "table_name": "keep", "column_name": "a"},
        {"table_schema": "s", "table_name": "drop_me", "column_name": "b"},
    ]
    connector = _connector(
        tables, columns, table_filter=from_connection({"table_deny_regex": "drop_me"})
    )

    out_tables, _, *_ = create_dataframe(connector)  # must not raise

    assert list(out_tables["table_name"]) == ["keep"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), ("true", True), ("TRUE", True), ("1", True)]
    + [(False, False), ("false", False), ("", False), (None, False), ("no", False)],
)
def test_exclude_views_reads_the_string_forms(raw, expected: bool) -> None:
    assert from_connection({"exclude_views": raw}).exclude_views is expected


def test_exclude_views_alone_makes_the_filter_active() -> None:
    assert from_connection({"exclude_views": True}).active is True
    assert from_connection({"exclude_views": False}).active is False


def test_exclude_views_is_a_filter_key_so_editing_it_re_ingests() -> None:
    from auto_ontology.catalog.table_filter import FILTER_KEYS

    assert "exclude_views" in FILTER_KEYS


def _views_fixture(**extra) -> SimpleNamespace:
    tables = [
        {
            "table_schema": "public",
            "table_name": "customer",
            "table_type": "base table",
        },
        {"table_schema": "public", "table_name": "customer_list", "table_type": "view"},
        {
            "table_schema": "public",
            "table_name": "sales_mat",
            "table_type": "materialized view",
        },
    ]
    columns = [
        {"table_schema": "public", "table_name": t["table_name"], "column_name": "id"}
        for t in tables
    ]
    return _connector(
        tables,
        columns,
        table_filter=from_connection({"exclude_views": True}),
        **extra,
    )


def test_exclude_views_drops_views_and_materialized_views_in_lockstep() -> None:
    out_tables, out_columns, *_ = create_dataframe(_views_fixture())

    assert list(out_tables["table_name"]) == ["customer"]
    # The coverage guard would raise if columns of a dropped view remained.
    assert list(out_columns["table_name"]) == ["customer"]


def test_exclude_views_also_drops_the_view_definitions() -> None:
    connector = _views_fixture()
    connector.get_views = lambda: pd.DataFrame(
        [
            {
                "table_schema": "public",
                "table_name": "customer_list",
                "view_definition": "select 1",
            }
        ]
    )

    _, _, out_views, *_ = create_dataframe(connector)

    assert out_views.empty


def test_exclude_views_finds_a_view_a_connector_only_lists_in_get_views() -> None:
    """A connector without ``table_type`` reports every relation as a table."""
    tables = [
        {"table_schema": "public", "table_name": "orders"},
        {"table_schema": "public", "table_name": "orders_v"},
    ]
    columns = [
        {"table_schema": "public", "table_name": "orders", "column_name": "id"},
        {"table_schema": "public", "table_name": "orders_v", "column_name": "id"},
    ]
    connector = _connector(
        tables, columns, table_filter=from_connection({"exclude_views": True})
    )
    connector.get_views = lambda: pd.DataFrame(
        [
            {
                "table_schema": "public",
                "table_name": "orders_v",
                "view_definition": "select 1",
            }
        ]
    )

    out_tables, *_ = create_dataframe(connector)

    assert list(out_tables["table_name"]) == ["orders"]


def test_exclude_views_tells_a_view_from_a_same_named_table_in_another_schema() -> None:
    tables = [
        {"table_schema": "a", "table_name": "report", "table_type": "view"},
        {"table_schema": "b", "table_name": "report", "table_type": "base table"},
    ]
    columns = [
        {"table_schema": "a", "table_name": "report", "column_name": "id"},
        {"table_schema": "b", "table_name": "report", "column_name": "id"},
    ]
    connector = _connector(
        tables, columns, table_filter=from_connection({"exclude_views": True})
    )

    out_tables, out_columns, *_ = create_dataframe(connector)

    assert list(out_tables["table_schema"]) == ["b"]
    assert list(out_columns["table_schema"]) == ["b"]


def test_exclude_views_combines_with_the_name_filter() -> None:
    connector = _views_fixture()
    connector.table_filter = from_connection(
        {"exclude_views": True, "table_deny_regex": "^customer$"}
    )

    out_tables, *_ = create_dataframe(connector)

    assert out_tables.empty


def test_without_the_switch_views_are_kept() -> None:
    connector = _views_fixture()
    connector.table_filter = from_connection({})

    out_tables, *_ = create_dataframe(connector)

    assert sorted(out_tables["table_name"]) == [
        "customer",
        "customer_list",
        "sales_mat",
    ]


def test_get_connectors_passes_the_filter_to_every_connector(monkeypatch) -> None:
    """The stored pattern has to reach the connectors ingestion actually uses.

    Regression: the filter was wired into the federated (SSO) connector path
    only, so `get_connectors` — which is what a triggered ingest resolves
    through — built connectors with no filter at all. The pattern was stored and
    displayed correctly, and ingestion quietly took every table anyway.
    """
    from auto_ontology.connectors import registry

    captured: dict = {}

    def fake_create_connector(cs, schemas=None, table_filter=None):
        captured["schemas"] = schemas
        captured["table_filter"] = table_filter
        return SimpleNamespace(database_name="db", close=lambda: None)

    monkeypatch.setattr(registry, "create_connector", fake_create_connector)
    monkeypatch.setattr(registry, "build_connection_string", lambda conn: "sqlite:///x")
    monkeypatch.setattr(
        "auto_ontology.dal.connections.list_connections",
        lambda: [{"database": "db", "type": "sqlite", "table_deny_regex": "^tmp_"}],
    )
    monkeypatch.setattr(registry, "_connectors", None)

    try:
        registry.get_connectors()
    finally:
        monkeypatch.setattr(registry, "_connectors", None)

    assert captured["table_filter"] is not None
    assert captured["table_filter"].active is True
    assert captured["table_filter"].keep("tmp_orders") is False
