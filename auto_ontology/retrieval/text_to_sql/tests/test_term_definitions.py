# SPDX-License-Identifier: Apache-2.0

from typing import Any, cast

import pytest

from auto_ontology.retrieval.text_to_sql.agents import candidates_retrieval
from auto_ontology.retrieval.text_to_sql.agents.candidates_retrieval import (
    CandidateRetrievalAgent,
)
from auto_ontology.retrieval.text_to_sql.state import AgentState
from auto_ontology.retrieval.text_to_sql.term_definitions import (
    BLOCK_HEADER,
    DefinedTerm,
    TermDefinitionSettings,
    build_term_definitions,
    expand_by_mentions,
    merge_evidence,
    render_block,
)
from auto_ontology.semantic.index_health import SemanticIndexHealth

CATALOGUE: list[dict[str, Any]] = [
    {
        "id": "t1",
        "name": "Signal-to-Noise Quality Indicator (SNQI)",
        "description": "SNQI = snr - 0.5 * noise. Uses the Link Margin.",
        "certified": False,
    },
    {
        "id": "t2",
        "name": "Link Margin",
        "description": "Margin over the floor; depends on the Antenna Gain.",
        "certified": False,
    },
    {
        "id": "t3",
        "name": "Antenna Gain",
        "description": "Gain in dBi.",
        "certified": True,
    },
    {
        "id": "t4",
        "name": "Unrelated",
        "description": "Nothing to see.",
        "certified": False,
    },
    {"id": "t5", "name": "ID", "description": "too short to match", "certified": False},
]


def _seeds(*ids: str) -> list[DefinedTerm]:
    by_id = {r["id"]: r for r in CATALOGUE}
    return [
        DefinedTerm(
            i, by_id[i]["name"], by_id[i]["description"], by_id[i]["certified"], 0
        )
        for i in ids
    ]


def _fake_search(rows: list[dict[str, Any]]) -> Any:
    calls: list[dict[str, Any]] = []

    def search(retriever: object, question: str, **kwargs: Any) -> list[dict]:
        calls.append({"question": question, **kwargs})
        return rows

    search.calls = calls  # type: ignore[attr-defined]
    return search


def test_expansion_follows_names_and_abbreviations_transitively() -> None:
    out = expand_by_mentions(_seeds("t1"), CATALOGUE, max_depth=2, max_terms=10)
    assert [t.name for t in out] == [
        "Signal-to-Noise Quality Indicator (SNQI)",
        "Link Margin",
        "Antenna Gain",
    ]
    assert [t.depth for t in out] == [0, 1, 2]


def test_abbreviation_in_a_definition_matches_the_parenthesised_name() -> None:
    seeds = [DefinedTerm("x", "Other", "Weighted by the snqi value.", False, 0)]
    out = expand_by_mentions(seeds, CATALOGUE, max_depth=1, max_terms=10)
    assert [t.id for t in out] == ["x", "t1"]


def test_expansion_is_bounded_by_depth_and_total() -> None:
    seeds = _seeds("t1")
    assert len(expand_by_mentions(seeds, CATALOGUE, max_depth=1, max_terms=10)) == 2
    assert len(expand_by_mentions(seeds, CATALOGUE, max_depth=5, max_terms=2)) == 2
    assert len(expand_by_mentions(seeds, CATALOGUE, max_depth=0, max_terms=10)) == 1


def test_whole_word_match_only_and_short_names_ignored() -> None:
    seeds = [DefinedTerm("x", "Other", "The ID of the Link Marginal thing.", False, 0)]
    out = expand_by_mentions(seeds, CATALOGUE, max_depth=2, max_terms=10)
    assert [t.id for t in out] == ["x"]


def test_cycles_terminate() -> None:
    cyc = [
        {"id": "a", "name": "Alpha", "description": "uses Beta", "certified": False},
        {"id": "b", "name": "Beta", "description": "uses Alpha", "certified": False},
    ]
    seeds = [DefinedTerm("a", "Alpha", "uses Beta", False, 0)]
    assert len(expand_by_mentions(seeds, cyc, max_depth=9, max_terms=9)) == 2


def test_render_puts_certified_first_and_respects_budget() -> None:
    terms = [
        DefinedTerm("a", "A", "x" * 50, False, 0),
        DefinedTerm("b", "B", "y" * 50, True, 1),
    ]
    block, kept = render_block(terms, 10_000)
    assert block.startswith(f"{BLOCK_HEADER}:\n- B: ")
    assert [t.id for t in kept] == ["b", "a"]
    budget = len(BLOCK_HEADER) + 2 + 60
    small, kept = render_block(terms, budget)
    assert [t.id for t in kept] == ["b"]
    assert len(small) <= budget
    assert render_block(terms, 5) == ("", [])


def test_render_skips_an_oversized_term_but_keeps_a_later_small_one() -> None:
    terms = [
        DefinedTerm("big", "Big", "z" * 500, False, 0),
        DefinedTerm("small", "Small", "ok", False, 1),
    ]
    _, kept = render_block(terms, 100)
    assert [t.id for t in kept] == ["small"]


def test_merge_keeps_request_evidence_first() -> None:
    assert merge_evidence("A means B.", "BLOCK") == "A means B.\n\nBLOCK"
    assert merge_evidence("", "BLOCK") == "BLOCK"
    assert merge_evidence("A means B.", "") == "A means B."


def test_build_retrieves_scopes_expands_and_renders() -> None:
    search = _fake_search(
        [
            {"id": "t1", "score": 0.4},
            {"id": "t4", "score": 0.9},
            {"id": "missing", "score": 0.1},
        ]
    )
    settings = TermDefinitionSettings(enabled=True, top_k=2, max_distance=0.5)
    defs = build_term_definitions(
        object(),
        "what is the SNQI?",
        "alien",
        settings,
        search=search,
        load_terms=lambda db: CATALOGUE if db == "alien" else [],
    )
    assert search.calls[0]["database_name"] == "alien"
    assert search.calls[0]["per_label_k"] == 2
    # t4 is beyond the threshold, "missing" is not in the database catalogue.
    assert [t.id for t in defs.rendered] == ["t3", "t1", "t2"]  # certified first
    assert defs.block.startswith(f"{BLOCK_HEADER}:\n")
    assert "- Antenna Gain: Gain in dBi." in defs.block
    assert defs.chars == len(defs.block)


def test_build_returns_nothing_without_hits_or_database() -> None:
    settings = TermDefinitionSettings(enabled=True)
    no_hits = build_term_definitions(
        object(),
        "q",
        "db",
        settings,
        search=_fake_search([]),
        load_terms=lambda _: CATALOGUE,
    )
    no_db = build_term_definitions(
        object(),
        "q",
        None,
        settings,
        search=_fake_search([{"id": "t1", "score": 0}]),
        load_terms=lambda _: CATALOGUE,
    )
    assert no_hits.block == "" and no_db.block == ""


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TERM_DEFINITIONS_IN_GENERATION",
        "TERM_DEFINITIONS_TOP_K",
        "TERM_DEFINITIONS_MAX_DISTANCE",
        "TERM_DEFINITIONS_MAX_DEPTH",
        "TERM_DEFINITIONS_MAX_TERMS",
        "TERM_DEFINITIONS_BUDGET_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert TermDefinitionSettings.from_env() == TermDefinitionSettings()
    assert TermDefinitionSettings().enabled is True
    monkeypatch.setenv("TERM_DEFINITIONS_IN_GENERATION", "false")
    assert TermDefinitionSettings.from_env().enabled is False
    monkeypatch.setenv("TERM_DEFINITIONS_IN_GENERATION", "true")
    monkeypatch.setenv("TERM_DEFINITIONS_TOP_K", "3")
    monkeypatch.setenv("TERM_DEFINITIONS_MAX_DISTANCE", "0.8")
    monkeypatch.setenv("TERM_DEFINITIONS_BUDGET_CHARS", "abc")
    s = TermDefinitionSettings.from_env()
    assert (s.enabled, s.top_k, s.max_distance, s.budget_chars) == (True, 3, 0.8, 6000)


# --- the retrieval agent --------------------------------------------------


@pytest.fixture(autouse=True)
def _quiet_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(candidates_retrieval, "_search_by_label", lambda *_a: [])
    monkeypatch.setattr(
        candidates_retrieval,
        "check_semantic_index",
        lambda database_name: SemanticIndexHealth(database_name, 0, 0),
    )
    monkeypatch.setattr(candidates_retrieval, "check_semantic_indexes", lambda: [])
    monkeypatch.setattr(
        candidates_retrieval, "custom_analysis_exists", lambda *_: False
    )


def _run(evidence: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "initial_question": "what is the SNQI?",
        "llm": object(),
        "path_state": {"entities": [], "target_db": "alien"},
        "semantic_retriever": object(),
    }
    if evidence is not None:
        state["evidence"] = evidence
    return CandidateRetrievalAgent().execute(cast(AgentState, state))


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_DEFINITIONS_IN_GENERATION", "true")
    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.term_definitions._default_search",
        _fake_search([{"id": "t3", "score": 0.1}]),
    )
    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.term_definitions._default_load_terms",
        lambda db: CATALOGUE,
    )


def test_flag_off_leaves_evidence_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_DEFINITIONS_IN_GENERATION", "false")
    assert set(_run("request evidence")) == {"path_state"}


def test_flag_on_appends_definitions_after_request_evidence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable(monkeypatch)
    with caplog.at_level("INFO"):
        result = _run("request evidence")
    evidence = result["evidence"]
    assert evidence.startswith("request evidence\n\n" + BLOCK_HEADER)
    assert "- Antenna Gain: Gain in dBi." in evidence
    assert any(
        "Antenna Gain" in r.getMessage() and "chars" in r.getMessage()
        for r in caplog.records
    )


def test_flag_on_without_request_evidence_is_just_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    assert _run(None)["evidence"].startswith(BLOCK_HEADER)


def test_rerun_on_the_same_state_does_not_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    first = _run("e")["evidence"]
    assert "evidence" not in _run(first)


def test_lookup_failure_falls_back_to_no_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("index down")

    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.term_definitions._default_search", boom
    )
    assert set(_run("e")) == {"path_state"}
