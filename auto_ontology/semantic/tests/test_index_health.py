# SPDX-License-Identifier: Apache-2.0

"""The comparison between the relational ontology and the semantic index."""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.semantic import index_health
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_TERM
from auto_ontology.semantic.index_health import (
    SemanticIndexHealth,
    check_semantic_index,
    check_semantic_indexes,
)


class _Index:
    """Stands in for the semantic VDB: ids stored per database."""

    def __init__(self, ids: dict[str, set[str]]) -> None:
        self.ids = ids
        self.asked: list[tuple[str, list[str]]] = []

    def ids_by_database(self, database_name: str, labels: list[str]) -> set[str]:
        self.asked.append((database_name, labels))
        return self.ids.get(database_name, set())


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    ontology: dict[str, tuple[set[str], set[str]]],
    indexed: dict[str, set[str]],
) -> _Index:
    index = _Index(indexed)
    monkeypatch.setattr(
        index_health,
        "fetch_semantic_node_ids_for_database",
        lambda database_name: ontology.get(database_name, (set(), set())),
    )
    monkeypatch.setattr(index_health, "get_semantic_vdb", lambda: index)
    monkeypatch.setattr(
        index_health, "fetch_databases", lambda: [{"name": name} for name in ontology]
    )
    return index


def test_an_empty_index_behind_a_full_ontology_is_all_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _patch(monkeypatch, {"pagila": ({"t1", "t2"}, {"a1", "a2", "a3"})}, {})

    health = check_semantic_index("pagila")

    assert health == SemanticIndexHealth("pagila", expected=5, indexed=0)
    assert health.missing == 5
    # Only the two labels the ontology tables back: the index also holds
    # SqlAttributes and analyses, which have no row in those tables.
    assert index.asked == [("pagila", [LABEL_TERM, LABEL_COLUMN_ATTRIBUTE])]


def test_rows_are_matched_by_id_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three rows for three nodes is not healthy if one belongs to a node that
    no longer exists: a count would call this complete."""
    _patch(
        monkeypatch,
        {"pagila": ({"t1"}, {"a1", "a2"})},
        {"pagila": {"t1", "a1", "deleted-attribute"}},
    )

    health = check_semantic_index("pagila")

    assert (health.expected, health.indexed, health.missing) == (3, 2, 1)


def test_a_database_with_no_ontology_is_not_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, {"empty": (set(), set())}, {})

    assert check_semantic_index("empty").missing == 0


def test_every_catalog_database_is_checked_on_its_own_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(
        monkeypatch,
        {"chinook": ({"t1"}, {"a1"}), "pagila": ({"t2"}, set())},
        {"chinook": {"t1", "a1"}, "pagila": set()},
    )

    by_name: dict[str, Any] = {h.database_name: h for h in check_semantic_indexes()}

    assert by_name["chinook"].missing == 0
    assert by_name["pagila"].missing == 1
