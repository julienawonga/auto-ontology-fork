# SPDX-License-Identifier: Apache-2.0

"""Evidence/discovery deadlock: reconstruction adds tables, validation rejects them.

Reproduces the "best customer" loop with fakes: evidence grounded on Customer
only, reconstruction discovers Invoice, the intent validator alternates between
"uses columns outside the evidence" and "has no ranking".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from auto_ontology.retrieval.text_to_sql.agents import sql_reconstruction
from auto_ontology.retrieval.text_to_sql.agents.response import ResponseAgent
from auto_ontology.retrieval.text_to_sql.agents.sql_parse_validation import (
    IntentValidationModel,
    SQLValidationAgent,
)
from auto_ontology.retrieval.text_to_sql.agents.sql_reconstruction import (
    ErrorAnalysis,
    ErrorType,
    SQLReconstructionAgent,
)
from auto_ontology.retrieval.text_to_sql.prompts import format_authoritative_evidence
from auto_ontology.retrieval.text_to_sql.state import AgentState
from auto_ontology.retrieval.text_to_sql.text_to_sql_graph import (
    MAX_RECONSTRUCTION_ATTEMPTS,
)

_MODULE = "auto_ontology.retrieval.text_to_sql.agents.sql_parse_validation"
_EVIDENCE = 'Customer refers to "Customer"."FirstName" and "Customer"."LastName".'
_ONLY_CUSTOMERS = 'SELECT "FirstName" FROM "Customer"'
_WITH_INVOICES = (
    'SELECT c."FirstName" FROM "Customer" c JOIN "Invoice" i '
    'ON i."CustomerId" = c."CustomerId" GROUP BY c."CustomerId" '
    'ORDER BY SUM(i."Total") DESC LIMIT 1'
)
_EVIDENCE_REJECTION = IntentValidationModel(
    is_valid=False,
    evidence_issues=["Invoice columns are not among the evidence targets."],
)
_NO_RANKING_REJECTION = IntentValidationModel(
    is_valid=False,
    aggregation_issues=["No metric or ranking identifies a best customer."],
)


def _validate(
    path_state: dict, sql: str, used_tables: list[str], verdict: IntentValidationModel
) -> dict:
    """Run one intent validation of ``sql`` with a fake LLM verdict."""
    path_state["sql_generation_result"] = SimpleNamespace(sql_code=sql)
    path_state["sql_columns"] = [f"cols:{sql[:10]}"]
    path_state["sql_tables"] = used_tables
    state = {
        "llm": MagicMock(),
        "evidence": _EVIDENCE,
        "initial_question": "Who is our best customer?",
        "connectors": [],
        "path_state": path_state,
    }
    with patch(f"{_MODULE}.invoke_with_structured_output", return_value=verdict):
        return SQLValidationAgent()._validate_intent(
            state, {**path_state, "sql_code": sql}, used_tables, []
        )


def test_oscillating_rejections_stop_within_the_attempt_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path_state: dict[str, Any] = {"relevant_tables": []}
    script = [
        (_ONLY_CUSTOMERS, ["public.Customer"], _NO_RANKING_REJECTION),
        (_WITH_INVOICES, ["public.Customer", "public.Invoice"], _EVIDENCE_REJECTION),
        (_ONLY_CUSTOMERS, ["public.Customer"], _NO_RANKING_REJECTION),
        (_WITH_INVOICES, ["public.Customer", "public.Invoice"], _EVIDENCE_REJECTION),
    ]
    decisions = []
    with caplog.at_level("WARNING"):
        for sql, tables, verdict in script:
            result = _validate(path_state, sql, tables, verdict)
            path_state = result["path_state"]
            decisions.append(result["decision"])
            if result["decision"] == "valid_sql":
                break

    # A, B, A: the third rejection repeats the first after a different one.
    assert decisions == ["invalid_sql", "invalid_sql", "valid_sql"]
    assert len(decisions) < MAX_RECONSTRUCTION_ATTEMPTS
    assert sum("oscillating" in r.getMessage() for r in caplog.records) == 1
    assert "not fully validated" in path_state["validation_caveat"]


def test_guard_returns_the_candidate_rejected_only_on_evidence_grounds() -> None:
    path_state: dict[str, Any] = {"relevant_tables": []}
    for sql, tables, verdict in [
        (_WITH_INVOICES, ["public.Customer", "public.Invoice"], _EVIDENCE_REJECTION),
        (_ONLY_CUSTOMERS, ["public.Customer"], _NO_RANKING_REJECTION),
        (_ONLY_CUSTOMERS, ["public.Customer"], _NO_RANKING_REJECTION),
    ]:
        result = _validate(path_state, sql, tables, verdict)
        path_state = result["path_state"]

    assert result["decision"] == "valid_sql"
    assert path_state["sql_code"] == _WITH_INVOICES
    assert path_state["sql_generation_result"].sql_code == _WITH_INVOICES


def test_distinct_rejections_do_not_trigger_the_guard() -> None:
    path_state: dict[str, Any] = {"relevant_tables": []}
    first = _validate(
        path_state, _ONLY_CUSTOMERS, ["public.Customer"], _NO_RANKING_REJECTION
    )
    second = _validate(
        first["path_state"],
        _WITH_INVOICES,
        ["public.Customer", "public.Invoice"],
        _EVIDENCE_REJECTION,
    )
    assert [first["decision"], second["decision"]] == ["invalid_sql", "invalid_sql"]
    assert "validation_caveat" not in second["path_state"]


def test_validator_is_told_discovered_tables_are_legitimate() -> None:
    path_state: dict[str, Any] = {
        "relevant_tables": [],
        "discovered_tables": ["Invoice", "InvoiceLine"],
        "sql_generation_result": SimpleNamespace(sql_code=_WITH_INVOICES),
    }
    captured: list = []

    def fake(_llm: object, messages: list, _model: object) -> IntentValidationModel:
        captured.extend(messages)
        return IntentValidationModel(is_valid=True)

    state = {
        "llm": MagicMock(),
        "evidence": _EVIDENCE,
        "initial_question": "Who is our best customer?",
        "connectors": [],
        "path_state": path_state,
    }
    with patch(f"{_MODULE}.invoke_with_structured_output", side_effect=fake):
        result = SQLValidationAgent()._validate_intent(
            state, {**path_state, "sql_code": _WITH_INVOICES}, [], []
        )

    assert result["decision"] == "valid_sql"
    evidence_message = next(m.content for m in captured if "Authoritative" in m.content)
    assert "Invoice, InvoiceLine" in evidence_message
    assert "legitimate and usable" in evidence_message
    assert "does not forbid other columns or tables" in evidence_message


def test_evidence_block_never_restricts_to_the_defined_terms() -> None:
    block = format_authoritative_evidence(_EVIDENCE)
    assert "does not forbid other columns or tables" in block
    assert "legitimate and usable" not in block
    assert block.endswith(_EVIDENCE)


def test_reconstruction_discovery_records_tables_and_declares_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list = []

    def fake_invoke(_llm: object, messages: list, model: object) -> Any:
        if model is ErrorAnalysis:
            return ErrorAnalysis(
                error_type=ErrorType.MISSING_DATA,
                explanation="Needs invoices.",
                search_queries=["invoice totals"],
            )
        captured.extend(messages)
        return None

    monkeypatch.setattr(
        sql_reconstruction, "invoke_with_structured_output", fake_invoke
    )
    monkeypatch.setattr(
        SQLReconstructionAgent,
        "_discover_tables",
        lambda self, state, queries, tables: [{"name": "Invoice", "columns": []}],
    )
    state = cast(
        AgentState,
        {
            "llm": MagicMock(),
            "initial_question": "Who is our best customer?",
            "evidence": _EVIDENCE,
            "messages": [],
            "path_state": {
                "error": "no ranking",
                "relevant_tables": [{"name": "Customer", "columns": []}],
                "sql_generation_result": SimpleNamespace(
                    sql_code=_ONLY_CUSTOMERS, thought=""
                ),
            },
        },
    )

    SQLReconstructionAgent().execute(state)

    assert state["path_state"]["discovered_tables"] == ["Invoice"]
    evidence_message = next(m.content for m in captured if "Authoritative" in m.content)
    assert "legitimate and usable: Invoice" in evidence_message


def test_response_appends_the_validation_caveat() -> None:
    state = cast(
        AgentState,
        {
            "messages": [],
            "path_state": {
                "sql_generation_result": SimpleNamespace(
                    sql_code=_WITH_INVOICES, response="Top customer."
                ),
                "validation_caveat": "Not fully validated.",
            },
        },
    )
    result = ResponseAgent().execute(state)
    assert (
        "> **Note:** Not fully validated."
        in (result["path_state"]["final_response"]["response"])
    )
