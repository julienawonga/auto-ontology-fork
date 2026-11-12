# SPDX-License-Identifier: Apache-2.0

"""The knowledge-use trace: building it, the used-heuristic, and its place in
the streamed ``result`` payload. Everything is a fake; no model or store runs."""

from __future__ import annotations

import importlib
import logging

from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from auto_ontology.retrieval.text_to_sql import knowledge_trace as kt
from auto_ontology.retrieval.text_to_sql.state import TextToSQLPayload
from auto_ontology.utils import llm_invoke

SQL = (
    "SELECT SUM(amount) - SUM(refund) AS net FROM sales.orders WHERE status = 'shipped'"
)


def _no_cert(_id: str) -> bool | None:
    return None


def _state(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"target_db": "shop"}
    base.update(extra)
    return base


def test_sql_attribute_expression_in_sql_is_used() -> None:
    state = _state(
        sql_attributes=[
            {
                "id": "a1",
                "name": "Net revenue",
                "expression": "sum(amount) - sum(refund)",
                "sql": "",
                "term_name": "Net Revenue",
            }
        ],
        retrieved_sql_attributes=[{"id": "a1", "database_name": "shop"}],
    )
    (entry,) = kt.build_knowledge_trace(state, SQL, certified_lookup=lambda _i: True)
    assert entry["kind"] == "sql_attribute"
    assert entry["used"] is True
    assert entry["certified"] is True
    assert entry["database"] == "shop"
    assert "refund" in entry["matched"]
    assert "Net Revenue" in entry["reason"]


def test_sql_attribute_absent_partial_and_no_sql() -> None:
    absent = {"id": "a2", "name": "x", "expression": "sum(tip) / count(visit_id)"}
    partial = {"id": "a3", "name": "y", "expression": "sum(amount) / count(visit_id)"}
    state = _state(sql_attributes=[absent, partial])
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(state, SQL, certified_lookup=_no_cert)
    }
    assert out["a2"]["used"] is False
    assert out["a3"]["used"] is None  # amount present, visit_id missing
    assert out["a3"]["matched"] == ["amount"]
    # without a final SQL nothing can be judged
    for e in kt.build_knowledge_trace(state, None, certified_lookup=_no_cert):
        assert e["used"] is None


def test_custom_analysis_whole_query_match() -> None:
    sql = "select  count(*)  from sales.orders where status='shipped'"
    state = _state(
        custom_analyses=[{"id": "c1", "name": "Shipped", "sql": sql.upper()}],
    )
    (entry,) = kt.build_knowledge_trace(state, sql, certified_lookup=_no_cert)
    assert (entry["kind"], entry["used"], entry["certified"]) == (
        "custom_analysis",
        True,
        None,
    )


def test_term_heuristic_uses_identifiers_and_literals_of_the_definition() -> None:
    terms = [
        {
            "id": "t1",
            "name": "Shipped order",
            "description": "An order whose `status` is 'shipped' in sales.orders.",
            "certified": True,
            "depth": 0,
        },
        {
            "id": "t2",
            "name": "Churned",
            "description": "Customer with cancelled_at set.",
            "certified": False,
            "depth": 1,
        },
        {
            "id": "t3",
            "name": "Prose only",
            "description": "Whatever the business means by it.",
            "certified": None,
            "depth": 0,
        },
    ]
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(
            _state(offered_terms=terms), SQL, certified_lookup=_no_cert
        )
    }
    assert out["t1"]["used"] is True and out["t1"]["certified"] is True
    assert "shipped" in out["t1"]["matched"]
    assert out["t2"]["used"] is False
    assert "depth 1" in out["t2"]["reason"]
    assert out["t3"]["used"] is None


def test_term_is_used_when_its_sql_attribute_is() -> None:
    state = _state(
        offered_terms=[
            {
                "id": "t1",
                "name": "Net Revenue",
                "description": "Prose.",
                "certified": True,
                "depth": 0,
            }
        ],
        sql_attributes=[
            {
                "id": "a1",
                "name": "net",
                "expression": "sum(amount) - sum(refund)",
                "term_name": "Net Revenue",
            }
        ],
    )
    out = {
        e["kind"]: e
        for e in kt.build_knowledge_trace(state, SQL, certified_lookup=_no_cert)
    }
    assert out["term"]["used"] is True


def test_column_attribute_is_true_or_undecided_never_false() -> None:
    state = _state(
        retrieved_column_attributes=[
            {"id": "k1", "name": "amount", "database_name": "shop"},
            {"id": "k2", "name": "Warehouse code", "database_name": "shop"},
        ]
    )
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(state, SQL, certified_lookup=_no_cert)
    }
    assert out["k1"]["used"] is True
    assert out["k2"]["used"] is None


def test_malformed_and_empty_state_is_tolerated() -> None:
    assert kt.build_knowledge_trace({}, "select 1") == []
    state = _state(sql_attributes=["junk", {"name": "no id"}], offered_terms=[None])
    assert kt.build_knowledge_trace(state, "select 1", certified_lookup=_no_cert) == []


def test_trace_does_not_mutate_the_state() -> None:
    state = _state(
        evidence="E",
        offered_terms=[{"id": "t", "name": "n", "description": "`a_b`", "depth": 0}],
    )
    before = repr(state)
    kt.build_knowledge_trace(state, SQL, certified_lookup=_no_cert)
    assert repr(state) == before


def test_summary_counts() -> None:
    trace = [
        {"kind": "term", "used": True, "certified": True},
        {"kind": "term", "used": False, "certified": True},
        {"kind": "sql_attribute", "used": None, "certified": None},
    ]
    line = kt.summarize(trace)
    assert "offered 3, used 1, undecided 1, certified-and-used 1" in line
    assert "term=2/1" in line


# --- a real case: definitions written as prose and math, no backticks ------

ALIEN_SQL = (
    "SELECT sc.category, AVG(signal.bwhz / (signal.centerfreqmhz * 1000000)) AS bfr, "
    "STDDEV(probability.anomscore) AS sd, "
    "AVG(probability.techsigprob * (1 - probability.natsrcprob) * "
    "probability.sigunique * (0.5 + (probability.anomscore / 10))) AS tols "
    "FROM signal JOIN probability ON probability.signalref = signal.signalid "
    "GROUP BY 1"
)
ALIEN_TABLES = [
    {
        "name": "signal",
        "columns": [
            {"name": n} for n in ("signalid", "bwhz", "centerfreqmhz", "status")
        ],
    },
    {
        "name": "probability",
        "columns": [
            {"name": n}
            for n in (
                "signalref",
                "techsigprob",
                "natsrcprob",
                "sigunique",
                "anomscore",
                "lastseen",
            )
        ],
    },
]


def _alien_terms() -> list[dict[str, Any]]:
    def term(i: str, name: str, desc: str) -> dict[str, Any]:
        return {
            "id": i,
            "name": name,
            "description": desc,
            "certified": True,
            "depth": 0,
        }

    return [
        term(
            "tols",
            "Technological Origin Likelihood Score (TOLS)",
            "TOLS = TechSigProb × (1 − NatSrcProb) × SigUnique × (0.5 + AnomScore/10)",
        ),
        term(
            "bfr1",
            "Bandwidth-Frequency Ratio (BFR)",
            "BFR = BWHz / (CenterFreqMHz × 10^6), a dimensionless ratio.",
        ),
        term(
            "bfr2",
            "Bandwidth-to-Frequency Ratio (BFR)",
            "Bandwidth in Hz divided by CenterFreqMHz converted to Hz.",
        ),
        term("neg", "Last Contact", "Time since LastSeen, in days."),
        term("prose", "Detection", "Whether the signal status is open."),
    ]


def _alien_state() -> dict[str, Any]:
    return _state(
        offered_terms=_alien_terms(),
        relevant_tables=ALIEN_TABLES,
        retrieved_column_attributes=[
            {"id": "ca1", "name": "Anomaly Score", "database_name": "alien"},
            {"id": "ca2", "name": "Bandwidth", "database_name": "alien"},
            {"id": "ca3", "name": "Last seen", "database_name": "alien"},
            {"id": "ca4", "name": "Mystery", "database_name": "alien"},
        ],
        column_attribute_contexts={
            "ca1": {"col_name": "AnomScore", "table_name": "probability"},
            "ca2": {"col_name": "BWHz", "table_name": "signal"},
            "ca3": {"col_name": "LastSeen", "table_name": "probability"},
        },
    )


def test_prose_and_math_definitions_are_matched_to_schema_identifiers() -> None:
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(
            _alien_state(), ALIEN_SQL, certified_lookup=_no_cert
        )
    }
    assert out["tols"]["used"] is True
    assert {"techsigprob", "natsrcprob", "sigunique", "anomscore"} <= set(
        out["tols"]["matched"]
    )
    assert out["bfr1"]["used"] is True and "bwhz" in out["bfr1"]["matched"]
    assert out["bfr2"]["used"] is True and out["bfr2"]["matched"] == ["centerfreqmhz"]
    # names a schema column (LastSeen) the SQL never touches
    assert out["neg"]["used"] is False
    # only an all-lowercase word that is a column name: not enough to decide
    assert out["prose"]["used"] is None


def test_column_attributes_match_the_physical_column_not_the_label() -> None:
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(
            _alien_state(), ALIEN_SQL, certified_lookup=_no_cert
        )
    }
    assert out["ca1"]["used"] is True and out["ca1"]["matched"] == ["anomscore"]
    assert out["ca2"]["used"] is True
    assert out["ca3"]["used"] is False  # known column, absent from the SQL
    assert out["ca4"]["used"] is None  # column unknown, label not in the SQL


def test_without_schema_information_bare_words_are_not_guessed() -> None:
    state = _alien_state()
    state.pop("relevant_tables")
    out = {
        e["id"]: e
        for e in kt.build_knowledge_trace(state, ALIEN_SQL, certified_lookup=_no_cert)
    }
    assert out["tols"]["used"] is None


# --- in the streamed result -------------------------------------------------


@pytest.fixture(name="main")
def main_fixture(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(llm_invoke, "get_llm_client", lambda **_k: None)
    monkeypatch.setattr(llm_invoke, "get_non_reasoning_llm_client", lambda **_k: None)
    return importlib.import_module("auto_ontology.retrieval.text_to_sql.main")


def _stream(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch, steps: list[dict[str, Any]]
) -> list[dict]:
    def items() -> Iterator[tuple[str, Any]]:
        for step in steps:
            yield ("updates", step)

    monkeypatch.setattr(main, "_build_state", lambda payload: {"path_state": {}})
    monkeypatch.setattr(
        main,
        "app",
        SimpleNamespace(stream=lambda _s, stream_mode=None, config=None: items()),
    )
    monkeypatch.setattr(kt, "_default_certified", _no_cert)
    return list(
        main.stream_agent_response(cast(TextToSQLPayload, {"question": "net revenue?"}))
    )


def _final_step(path_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_response": {
            "path_state": {
                **path_state,
                "final_response": {"response": "ok", "sql_code": SQL},
            }
        }
    }


def test_result_payload_carries_the_trace_and_logs_a_summary(
    main: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = _final_step(
        _state(
            sql_attributes=[
                {"id": "a1", "name": "net", "expression": "sum(amount) - sum(refund)"}
            ]
        )
    )
    with caplog.at_level(logging.INFO):
        events = _stream(main, monkeypatch, [step])
    (result,) = [e for e in events if e["type"] == "result"]
    trace = result["answer"]["knowledge_trace"]
    assert [(e["kind"], e["id"], e["used"]) for e in trace] == [
        ("sql_attribute", "a1", True)
    ]
    assert any(
        r.getMessage().startswith("Knowledge trace: offered 1, used 1")
        for r in caplog.records
    )


def test_trace_failure_never_costs_the_answer(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> list:
        raise RuntimeError("bad state")

    monkeypatch.setattr(main, "build_knowledge_trace", boom)
    events = _stream(main, monkeypatch, [_final_step({})])
    (result,) = [e for e in events if e["type"] == "result"]
    assert result["answer"]["sql_code"] == SQL
    assert "knowledge_trace" not in result["answer"]
