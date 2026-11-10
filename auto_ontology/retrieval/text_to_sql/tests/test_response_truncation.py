# SPDX-License-Identifier: Apache-2.0

"""A result cut at the row limit must not be presented as a complete answer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from auto_ontology.retrieval.text_to_sql.agents.response import ResponseAgent


def _run(**path_state: Any) -> dict[str, Any]:
    state = {
        "messages": [],
        "path_state": {
            "sql_generation_result": SimpleNamespace(
                sql_code="SELECT 1", response="Total revenue is 100."
            ),
            "candidates": [],
            **path_state,
        },
    }
    return ResponseAgent().execute(state)["path_state"]["final_response"]


def test_a_cut_result_is_disclosed_in_the_answer() -> None:
    final = _run(result_truncated_at=10_000)

    assert "limited to the first 10,000 rows" in final["response"]
    assert "may be incomplete" in final["response"]
    assert final["result_truncated_at"] == 10_000


def test_a_complete_result_gets_no_note() -> None:
    final = _run(result_truncated_at=None)

    assert "limited" not in final["response"]
    assert final["result_truncated_at"] is None
