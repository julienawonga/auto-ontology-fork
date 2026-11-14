# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for SqlAttribute embedding database selection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from auto_ontology.server.sql_attributes import service


@pytest.fixture
def harness(monkeypatch):
    """Replace persistence and SQL validation while retaining service flow."""
    monkeypatch.setattr(service, "find_attr_by_name", lambda *a, **k: None)
    monkeypatch.setattr(service, "find_attr_by_expression", lambda *a, **k: None)
    monkeypatch.setattr(
        service, "get_slim_term_by_id", lambda _id: {"id": "term-1", "name": "Term"}
    )
    monkeypatch.setattr(
        service,
        "validate_sql",
        lambda *a, **k: SimpleNamespace(
            sql_node=SimpleNamespace(match_props={}), edges=[], get_edges=lambda: []
        ),
    )
    monkeypatch.setattr(
        service,
        "_persist_attr_with_sql",
        lambda node, sql, query: {
            "id": "attr-1",
            "name": node.get_properties()["name"],
        },
    )
    monkeypatch.setattr(service, "link_to_term", lambda *a: None)
    monkeypatch.setattr(service, "get_dialects", lambda *_: [])
    monkeypatch.setattr(service, "get_schemas", lambda *_: [])
    monkeypatch.setattr(service, "get_embed_params", lambda: object())
    embed = MagicMock()
    monkeypatch.setattr(service, "_embed_sql_attribute", embed)
    monkeypatch.setattr(service, "get_semantic_vdb", lambda: object())
    monkeypatch.setattr(
        service,
        "get_connectors",
        lambda: [
            SimpleNamespace(database_name="only_db"),
            SimpleNamespace(database_name="other_db"),
        ],
    )
    monkeypatch.setattr(service, "fetch_database_name_for_term", lambda _id: "term_db")
    return embed


def test_create_uses_term_database_with_multiple_connectors(harness) -> None:
    row = service.create_sql_attribute(
        name="A", description="", expression="SELECT 1", term_id="term-1"
    )
    assert harness.call_args.kwargs["database_name"] == "term_db"
    assert row["database_name"] == "term_db"


def test_explicit_connector_overrides_term_database(harness, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_resolve_connector",
        lambda name: {"OTHER": "other_db"}[name],
    )
    row = service.create_sql_attribute(
        name="A",
        description="",
        expression="SELECT 1",
        term_id="term-1",
        connector="OTHER",
    )
    assert harness.call_args.kwargs["database_name"] == "other_db"
    assert row["database_name"] == "other_db"


def test_unknown_term_database_falls_back_to_single_connector(
    harness, monkeypatch
) -> None:
    monkeypatch.setattr(
        service, "get_connectors", lambda: [SimpleNamespace(database_name="only_db")]
    )
    monkeypatch.setattr(service, "fetch_database_name_for_term", lambda _id: None)
    row = service.create_sql_attribute(
        name="A", description="", expression="SELECT 1", term_id="term-1"
    )
    assert harness.call_args.kwargs["database_name"] == "only_db"
    assert row["database_name"] == "only_db"


def test_metadata_name_patch_reembeds_with_term_database(harness, monkeypatch) -> None:
    existing = {
        "id": "attr-1",
        "name": "Old",
        "description": "",
        "term_id": "term-1",
        "owner_id": None,
    }
    monkeypatch.setattr(service, "get_full_sql_attribute_by_id", lambda _id: existing)
    monkeypatch.setattr(
        service, "clear_sql_attribute_description_suggestion", lambda *_: None
    )
    monkeypatch.setattr(
        service.sql_attr_dal, "update_sql_attribute", lambda *a, **k: None
    )
    monkeypatch.setattr(
        service,
        "_reembed_sql_attribute",
        lambda attr_id, database_name: harness(
            attr_id=attr_id, database_name=database_name
        ),
    )
    monkeypatch.setattr(
        service,
        "get_full_sql_attribute_by_id",
        MagicMock(side_effect=[existing, {**existing, "name": "New"}]),
    )

    service.update_sql_attribute(attr_id="attr-1", name="New")

    assert harness.call_args.kwargs["database_name"] == "term_db"
