# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import httpx

from dev_tools.evals.cases import Case, GoldAnswer
from dev_tools.evals.runner import (
    answer_from_events,
    ask,
    parse_sse,
    rows_from_result,
    score,
)


def _frames(*events: object) -> list[str]:
    lines = [f"data: {json.dumps(e)}" for e in events]
    return lines + ["data: [DONE]"]


def test_parse_sse_stops_at_done_and_skips_noise() -> None:
    lines = [
        "",
        ": keepalive",
        'data: {"type": "step"}',
        "data: not json",
        "data: [DONE]",
        'data: {"type": "late"}',
    ]
    assert [e["type"] for e in parse_sse(lines)] == ["step"]


def test_rows_from_json_string_payload() -> None:
    payload = json.dumps([{"title": "A", "revenue": 1.5}, {"title": "B", "revenue": 2}])
    assert rows_from_result({"sql_response_from_db": [payload]}) == [
        ["A", 1.5],
        ["B", 2],
    ]


def test_rows_from_dict_list_and_empty_payload() -> None:
    assert rows_from_result({"sql_response_from_db": [{"n": 599}]}) == [[599]]
    assert rows_from_result({"sql_response_from_db": ["[]"]}) == []


def test_no_sql_is_none_not_empty() -> None:
    assert rows_from_result({"response": "I cannot tell"}) is None


def test_newest_result_wins_and_sql_is_kept() -> None:
    answer = answer_from_events(
        [
            {"type": "sql", "sql": "SELECT 1"},
            {
                "type": "result",
                "answer": {
                    "sql_code": "SELECT 2",
                    "sql_response_from_db": [{"x": 2}],
                    "response": "two",
                },
            },
        ]
    )
    assert answer.sql == "SELECT 2" and answer.rows == [[2]] and answer.text == "two"


def _case(**kw: object) -> Case:
    gold = (GoldAnswer("primary", "SELECT 1", [["Drama", 104.79]]),)
    return Case(id="c", question="q", gold=gold, **kw)  # type: ignore[arg-type]


def test_score_passes_on_matching_rows_and_ignores_sql_text() -> None:
    run = score(
        _case(),
        answer_from_events(
            [
                {
                    "type": "result",
                    "answer": {
                        "sql_code": "anything",
                        "sql_response_from_db": [{"cat": "drama", "r": 104.79}],
                    },
                }
            ]
        ),
    )
    assert run.passed and run.matched == "primary"


def test_score_error_with_no_rows_is_an_error_not_a_value_miss() -> None:
    run = score(_case(), answer_from_events([{"type": "error", "message": "boom"}]))
    assert not run.passed and run.reason == "error" and run.error == "boom"


def test_ask_reads_the_stream_over_http() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        stream = "\n".join(
            _frames(
                {"type": "result", "answer": {"sql_response_from_db": [{"n": 599}]}}
            )
        )
        return httpx.Response(200, text=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = ask(
            client, "http://backend:3001/", "How many customers?", target_db="pagila"
        )

    assert answer.rows == [[599]]
    assert sent["body"] == {"question": "How many customers?", "target_db": "pagila"}


def test_ask_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="semantic layer missing")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = ask(client, "http://backend:3001", "q")
    assert answer.error is not None and answer.error.startswith("HTTP 409")
