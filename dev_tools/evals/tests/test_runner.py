# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import httpx

import pytest

from dev_tools.evals.cases import Case, GoldAnswer
from dev_tools.evals.report import CaseResult, Report, Run
from dev_tools.evals.runner import (
    Answer,
    answer_from_events,
    ask,
    parse_sse,
    rescore,
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


def test_ask_sends_case_evidence_with_the_question() -> None:
    """Benchmark definitions travel as the chat's ``evidence`` field."""
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, text="\n".join(_frames({"type": "result"})))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ask(client, "http://backend:3001", "Count VIPs", evidence="VIP: spend > 15")

    assert sent["body"] == {"question": "Count VIPs", "evidence": "VIP: spend > 15"}


def test_ask_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="semantic layer missing")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = ask(client, "http://backend:3001", "q")
    assert answer.error is not None and answer.error.startswith("HTTP 409")


def test_a_scored_run_keeps_the_rows_it_was_judged_on() -> None:
    run = score(_case(), Answer(rows=[["Drama", 104.79]]))

    assert run.actual == [["Drama", 104.79]]


def test_stored_rows_are_capped() -> None:
    rows = [[i] for i in range(500)]

    assert len(score(_case(), Answer(rows=rows)).actual or []) == 50


def _report(*runs: Run) -> Report:
    return Report(meta={"label": "r"}, cases={"c": CaseResult("q", list(runs))})


def test_rescore_changes_the_verdict_when_the_convention_changes() -> None:
    """A narrower answer passes only where the case tolerates a missing column."""
    narrow = score(_case(allow_missing_columns=True), Answer(rows=[["Drama"]]))
    assert narrow.passed
    report = _report(narrow)

    as_declared = rescore(report, [_case(allow_missing_columns=True)])
    strict = rescore(report, [_case(allow_missing_columns=True)], strict=True)

    assert as_declared.cases["c"].pass_rate == 1.0
    assert strict.cases["c"].pass_rate == 0.0
    assert strict.cases["c"].runs[0].reason == "column_count"
    assert strict.meta["rescored"] == "strict"


def test_rescore_keeps_runs_that_failed_before_producing_rows() -> None:
    failed = Run(False, "error", error="HTTP 500")

    out = rescore(_report(failed), [_case()])

    assert out.cases["c"].runs == [failed]


def test_rescore_refuses_a_report_saved_without_rows() -> None:
    legacy = Run(True, "match", rows=1)  # written before rows were stored

    with pytest.raises(ValueError, match="without the rows"):
        rescore(_report(legacy), [_case()])


def test_rescore_rejects_a_case_missing_from_the_cases_file() -> None:
    with pytest.raises(ValueError, match="not in the cases file"):
        rescore(_report(Run(True, "match", actual=[["x"]])), [])


def _busy_then_ok(retry_after: str | None, busy: int) -> httpx.MockTransport:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= busy:
            headers = {"Retry-After": retry_after} if retry_after else {}
            return httpx.Response(503, text="busy", headers=headers)
        return httpx.Response(
            200, text="\n".join(_frames({"type": "result", "answer": {}}))
        )

    return httpx.MockTransport(handler)


def test_ask_retries_a_503_and_honours_retry_after() -> None:
    slept: list[float] = []
    with httpx.Client(transport=_busy_then_ok("7", 2)) as client:
        answer = ask(client, "http://backend:3001", "q", sleep=slept.append)

    assert slept == [7.0, 7.0]
    assert answer.busy_waits == [7.0, 7.0]
    assert answer.error is None


def test_ask_gives_up_after_the_retry_budget() -> None:
    slept: list[float] = []
    with httpx.Client(transport=_busy_then_ok(None, 99)) as client:
        answer = ask(
            client, "http://backend:3001", "q", max_busy_retries=3, sleep=slept.append
        )

    assert slept == [5.0, 5.0, 5.0]  # no header -> the default delay
    assert answer.error is not None and answer.error.startswith("HTTP 503")


def test_ask_stops_retrying_once_the_total_wait_budget_is_spent() -> None:
    slept: list[float] = []
    with httpx.Client(transport=_busy_then_ok("40", 99)) as client:
        answer = ask(
            client, "http://backend:3001", "q", max_busy_wait_s=100, sleep=slept.append
        )

    assert slept == [40.0, 40.0]
    assert answer.error is not None and answer.error.startswith("HTTP 503")


def test_run_cases_records_the_waits_in_the_report_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev_tools.evals import runner

    def fake_ask(*args: object, **kwargs: object) -> Answer:
        return Answer(rows=[[1]], busy_waits=[5.0, 5.0])

    monkeypatch.setattr(runner, "ask", fake_ask)
    report = runner.run_cases([_case()], "http://backend:3001", label="x", repeats=2)

    assert report.meta["busy_retries"] == 4
    assert report.meta["busy_wait_s"] == 20.0


def test_warn_if_over_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dev_tools.evals import __main__ as cli

    monkeypatch.setattr(cli, "backend_chat_cap", lambda url: 4)
    cli.warn_if_over_cap("http://x", 6)
    assert "exceeds the backend's chat cap of 4" in capsys.readouterr().err
    cli.warn_if_over_cap("http://x", 4)
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(cli, "backend_chat_cap", lambda url: None)
    cli.warn_if_over_cap("http://x", 6)
    assert capsys.readouterr().err == ""
