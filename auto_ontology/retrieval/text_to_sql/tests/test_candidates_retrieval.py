# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import Any, cast

import pytest

from auto_ontology.retrieval.text_to_sql.agents import candidates_retrieval
from auto_ontology.retrieval.text_to_sql.agents.candidates_retrieval import (
    CandidateRetrievalAgent,
    _select_candidate_database,
)
from auto_ontology.retrieval.text_to_sql.state import AgentState
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_SQL_ATTRIBUTE
from auto_ontology.semantic.index_health import SemanticIndexHealth


@pytest.fixture(autouse=True)
def healthy_semantic_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty retrieval consults the index health, which reads the store."""
    monkeypatch.setattr(
        candidates_retrieval,
        "check_semantic_index",
        lambda database_name: SemanticIndexHealth(database_name, 0, 0),
    )
    monkeypatch.setattr(candidates_retrieval, "check_semantic_indexes", lambda: [])


def _hit(
    hit_id: str,
    database_name: str | None,
    score: float,
    *,
    query_entity: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "id": hit_id,
        "text": hit_id,
        "score": score,
    }
    if database_name is not None:
        hit["database_name"] = database_name
    if query_entity is not None:
        hit["query_entity"] = query_entity
    if source is not None:
        hit["source"] = source
    return hit


def _state(
    entities: list[str],
    *,
    target_db: str | None = None,
    evidence: str | None = None,
) -> AgentState:
    path_state: dict[str, Any] = {"entities": entities}
    if target_db is not None:
        path_state["target_db"] = target_db
    state = {
        "initial_question": "question",
        "llm": object(),
        "path_state": path_state,
        "semantic_retriever": object(),
    }
    if evidence is not None:
        state["evidence"] = evidence
    return cast(AgentState, state)


def test_selects_database_by_entity_coverage_before_total_hits() -> None:
    column_hits = [
        _hit("a-revenue", "db-a", 0.3, query_entity="revenue"),
        _hit("a-region", "db-a", 0.4, query_entity="region"),
        _hit("b-revenue-1", "db-b", 0.1, query_entity="revenue"),
        _hit("b-revenue-2", "db-b", 0.1, query_entity="revenue"),
    ]
    custom_hits = [
        _hit("b-custom-1", "db-b", 0.1),
        _hit("b-custom-2", "db-b", 0.1),
    ]

    selected, _ = _select_candidate_database(column_hits, custom_hits, [])

    assert selected == "db-a"


@pytest.mark.parametrize(
    ("column_hits", "custom_hits", "expected"),
    [
        (
            [
                _hit("a", "db-a", 0.3, query_entity="revenue"),
                _hit("b", "db-b", 0.3, query_entity="revenue"),
            ],
            [_hit("b-custom", "db-b", 0.9)],
            "db-b",
        ),
        (
            [
                _hit("a", "db-a", 0.2, query_entity="revenue"),
                _hit("b", "db-b", 0.4, query_entity="revenue"),
            ],
            [],
            "db-a",
        ),
        (
            [
                _hit("a", "db-a", 0.2, query_entity="revenue"),
                _hit("b", "db-b", 0.2, query_entity="revenue"),
            ],
            [],
            "db-a",
        ),
    ],
)
def test_database_selection_ties_are_deterministic(
    column_hits: list[dict],
    custom_hits: list[dict],
    expected: str,
) -> None:
    selected, _ = _select_candidate_database(column_hits, custom_hits, [])

    assert selected == expected


def test_unscoped_retrieval_filters_database_and_backfills_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def fake_search(
        _retriever: object,
        entity: str,
        label: str,
        _k: int,
        database_name: str | None = None,
    ) -> list[dict]:
        calls.append((entity, label, database_name))
        if database_name == "db-a":
            assert label == LABEL_COLUMN_ATTRIBUTE
            return [_hit(f"a-{entity}", "db-a", 0.1)]
        if label == LABEL_COLUMN_ATTRIBUTE:
            initial_hits = {
                "revenue": [
                    _hit("a-revenue", "db-a", 0.2),
                    _hit("b-revenue", "db-b", 0.1),
                    _hit("missing-db", None, 0.01),
                ],
                "region": [
                    _hit("a-region", "db-a", 0.2),
                    _hit("b-region", "db-b", 0.1),
                ],
                "customer": [_hit("missing-customer-db", None, 0.1)],
            }
            return initial_hits[entity]
        if label == LABEL_SQL_ATTRIBUTE:
            return [
                _hit("a-sql", "db-a", 0.2),
                _hit("b-sql", "db-b", 0.1),
            ]
        return [
            _hit("a-custom", "db-a", 0.2),
            _hit("a-custom-2", "db-a", 0.3),
            _hit("b-custom", "db-b", 0.1),
        ]

    filtered_inputs: dict[str, list[dict]] = {}

    def fake_filter(
        _llm: object,
        _question: str,
        custom_hits: list[dict],
        sql_hits: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        filtered_inputs["custom"] = custom_hits
        filtered_inputs["sql"] = sql_hits
        return custom_hits, sql_hits

    monkeypatch.setattr(candidates_retrieval, "_search_by_label", fake_search)
    monkeypatch.setattr(candidates_retrieval, "_llm_filter_both", fake_filter)
    monkeypatch.setattr(candidates_retrieval, "custom_analysis_exists", lambda *_: True)

    result = CandidateRetrievalAgent().execute(
        _state(["revenue", "region", "customer"])
    )
    path_state = result["path_state"]

    assert path_state["target_db"] == "db-a"
    for key in (
        "retrieved_column_attributes",
        "retrieved_custom_analyses",
        "retrieved_sql_attributes",
    ):
        assert {hit["database_name"] for hit in path_state[key]} == {"db-a"}

    assert filtered_inputs["custom"] == [
        _hit("a-custom", "db-a", 0.2),
        _hit("a-custom-2", "db-a", 0.3),
    ]
    assert filtered_inputs["sql"] == [_hit("a-sql", "db-a", 0.2)]
    assert ("customer", LABEL_COLUMN_ATTRIBUTE, "db-a") in calls
    assert not any(
        entity in {"revenue", "region"} and database_name == "db-a"
        for entity, label, database_name in calls
        if label == LABEL_COLUMN_ATTRIBUTE
    )

    column_by_id = {hit["id"]: hit for hit in path_state["retrieved_column_attributes"]}
    assert column_by_id["a-customer"]["query_entities"] == ["customer"]
    assert "missing-db" not in column_by_id


def test_all_bridge_sourced_true_when_every_hit_is_bridge() -> None:
    hits = [
        _hit("bridge-1", "db-a", 0.1, source="bridgeTable"),
        _hit("bridge-2", "db-a", 0.2, source="bridgeTable"),
    ]

    assert candidates_retrieval._all_bridge_sourced(hits) is True


@pytest.mark.parametrize(
    "sources",
    [
        ("bridgeTable", "sql"),
        ("sql", "sql"),
        (None, None),
    ],
)
def test_all_bridge_sourced_false_for_mixed_missing_or_non_bridge(
    sources: tuple[str | None, str | None],
) -> None:
    hits = [
        _hit("a", "db-a", 0.1, source=sources[0]),
        _hit("b", "db-a", 0.2, source=sources[1]),
    ]

    assert candidates_retrieval._all_bridge_sourced(hits) is False


def test_all_bridge_sourced_false_for_empty_hits() -> None:
    assert candidates_retrieval._all_bridge_sourced([]) is False


def test_bridge_sourced_sql_attrs_skip_llm_filter_but_custom_still_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_search(
        _retriever: object,
        entity: str,
        label: str,
        _k: int,
        database_name: str | None = None,
    ) -> list[dict]:
        if label == LABEL_SQL_ATTRIBUTE:
            return [_hit("bridge-1", "db-a", 0.1, source="bridgeTable")]
        if label == LABEL_COLUMN_ATTRIBUTE:
            return [_hit(f"a-{entity}", "db-a", 0.1)]
        return [_hit("a-custom", "db-a", 0.2)]

    both_calls: list[object] = []
    single_calls: list[tuple[list[dict], str]] = []

    monkeypatch.setattr(candidates_retrieval, "_search_by_label", fake_search)
    monkeypatch.setattr(
        candidates_retrieval,
        "_llm_filter_both",
        lambda *args: both_calls.append(args) or (args[2], args[3]),
    )

    def fake_single_filter(
        _llm: object, _question: str, candidates: list[dict], candidate_type: str
    ) -> list[dict]:
        single_calls.append((candidates, candidate_type))
        return candidates

    monkeypatch.setattr(
        candidates_retrieval, "_llm_filter_candidates", fake_single_filter
    )

    result = CandidateRetrievalAgent().execute(_state(["revenue"], target_db="db-a"))

    assert not both_calls
    assert len(single_calls) == 1
    assert single_calls[0][1] == "custom analyses"
    assert [h["id"] for h in result["path_state"]["retrieved_sql_attributes"]] == [
        "bridge-1"
    ]


def test_explicit_target_db_preserves_existing_search_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def fake_search(
        _retriever: object,
        entity: str,
        label: str,
        _k: int,
        database_name: str | None = None,
    ) -> list[dict]:
        calls.append((entity, label, database_name))
        return [_hit(f"{label}-{entity}", "db-a", 0.2)]

    monkeypatch.setattr(candidates_retrieval, "_search_by_label", fake_search)
    monkeypatch.setattr(
        candidates_retrieval,
        "_llm_filter_both",
        lambda _llm, _question, custom, sql: (custom, sql),
    )
    monkeypatch.setattr(candidates_retrieval, "custom_analysis_exists", lambda *_: True)

    result = CandidateRetrievalAgent().execute(
        _state(["revenue", "region"], target_db="db-a")
    )

    assert len(calls) == 4
    assert {database_name for _, _, database_name in calls} == {"db-a"}
    assert result["path_state"]["target_db"] == "db-a"
    assert len(result["path_state"]["retrieved_column_attributes"]) == 2


def test_full_evidence_adds_column_attribute_vector_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = "Eligible rate equals eligible count divided by total enrollment."
    calls: list[tuple[str, str, int, str | None]] = []

    def fake_search(
        _retriever: object,
        query: str,
        label: str,
        k: int,
        database_name: str | None = None,
    ) -> list[dict]:
        calls.append((query, label, k, database_name))
        if label == LABEL_COLUMN_ATTRIBUTE:
            score = 0.1 if query == evidence else 0.2
            return [_hit("eligible-rate", "db-a", score)]
        return []

    monkeypatch.setattr(candidates_retrieval, "_search_by_label", fake_search)
    monkeypatch.setattr(
        candidates_retrieval,
        "_llm_filter_both",
        lambda _llm, _question, custom, sql: (custom, sql),
    )
    monkeypatch.setattr(
        candidates_retrieval, "custom_analysis_exists", lambda *_: False
    )

    result = CandidateRetrievalAgent().execute(
        _state(["eligible rate"], target_db="db-a", evidence=evidence)
    )

    assert (evidence, LABEL_COLUMN_ATTRIBUTE, 3, "db-a") in calls
    hits = result["path_state"]["retrieved_column_attributes"]
    assert len(hits) == 1
    assert hits[0]["id"] == "eligible-rate"
    assert hits[0]["query_evidence"] == evidence
    assert hits[0]["query_entities"] == ["eligible rate"]


# ---------------------------------------------------------------------------
# An empty retrieval over an index that was never filled
# ---------------------------------------------------------------------------


def _retrieve_nothing(
    monkeypatch: pytest.MonkeyPatch, *, target_db: str | None
) -> None:
    monkeypatch.setattr(candidates_retrieval, "_search_by_label", lambda *_a: [])
    monkeypatch.setattr(
        candidates_retrieval,
        "_llm_filter_both",
        lambda _llm, _question, custom, sql: (custom, sql),
    )
    monkeypatch.setattr(
        candidates_retrieval, "custom_analysis_exists", lambda *_: False
    )
    CandidateRetrievalAgent().execute(_state(["rentals"], target_db=target_db))


def test_an_empty_index_behind_existing_terms_is_warned_about(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Retrieval returning nothing is silent by design -- the chat falls back to
    the raw schema -- which is how an index holding no row at all went unseen."""
    monkeypatch.setattr(
        candidates_retrieval,
        "check_semantic_index",
        lambda database_name: SemanticIndexHealth(database_name, 80, 0),
    )

    with caplog.at_level(logging.WARNING):
        _retrieve_nothing(monkeypatch, target_db="pagila")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "Semantic index is empty for database 'pagila'" in w and "80" in w
        for w in warnings
    )


def test_every_database_is_checked_when_none_was_selected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        candidates_retrieval,
        "check_semantic_indexes",
        lambda: [
            SemanticIndexHealth("chinook", 12, 12),
            SemanticIndexHealth("pagila", 80, 0),
        ],
    )

    with caplog.at_level(logging.WARNING):
        _retrieve_nothing(monkeypatch, target_db=None)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Semantic index is empty for database 'pagila'" in text
    assert "'chinook'" not in text


def test_no_hits_from_a_populated_index_is_not_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Some questions match nothing; that is not a fault to report."""
    monkeypatch.setattr(
        candidates_retrieval,
        "check_semantic_index",
        lambda database_name: SemanticIndexHealth(database_name, 80, 80),
    )

    with caplog.at_level(logging.WARNING):
        _retrieve_nothing(monkeypatch, target_db="pagila")

    assert "Semantic index is empty" not in caplog.text


def test_the_index_is_not_checked_when_retrieval_found_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check costs queries; a working chat must not pay for it."""

    def _unexpected(database_name: str) -> SemanticIndexHealth:
        raise AssertionError("index health checked on a non-empty retrieval")

    monkeypatch.setattr(candidates_retrieval, "check_semantic_index", _unexpected)
    monkeypatch.setattr(
        candidates_retrieval,
        "_search_by_label",
        lambda _r, _q, label, _k, _db=None: (
            [_hit("rental-date", "pagila", 0.1)]
            if label == LABEL_COLUMN_ATTRIBUTE
            else []
        ),
    )
    monkeypatch.setattr(
        candidates_retrieval,
        "_llm_filter_both",
        lambda _llm, _question, custom, sql: (custom, sql),
    )
    monkeypatch.setattr(
        candidates_retrieval, "custom_analysis_exists", lambda *_: False
    )

    result = CandidateRetrievalAgent().execute(_state(["rentals"], target_db="pagila"))

    assert len(result["path_state"]["retrieved_column_attributes"]) == 1


def test_a_failing_health_check_does_not_fail_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreachable(database_name: str) -> SemanticIndexHealth:
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(candidates_retrieval, "check_semantic_index", _unreachable)

    _retrieve_nothing(monkeypatch, target_db="pagila")
