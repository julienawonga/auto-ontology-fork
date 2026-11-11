# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from dev_tools.evals import __main__ as cli
from dev_tools.evals import replay as replay_mod
from dev_tools.evals import runner
from dev_tools.evals.cases import Case, GoldAnswer
from dev_tools.evals.replay import apply_replay, replay_report, select_replay
from dev_tools.evals.report import CaseResult, Report, Run
from dev_tools.evals.runner import MAX_WORKERS, Answer, Job, clamp_workers, run_jobs


def _case(cid: str, db: str = "") -> Case:
    return Case(cid, f"question {cid}", (GoldAnswer("g", "S", [[1]]),), target_db=db)


def _run(
    passed: bool = False,
    sql: str | None = None,
    seconds: float = 0.1,
    reason: str = "x",
) -> Run:
    return Run(passed, reason, sql=sql, seconds=seconds)


def _report(label: str = "eng") -> Report:
    report = Report(meta={"label": label, "backend_url": "http://x"})
    report.cases = {
        "infra": CaseResult("q", [_run()]),  # no sql, instant: replay
        "slow": CaseResult("q", [_run(seconds=30.0)]),  # no sql but took long
        "wrong": CaseResult("q", [_run(sql="SELECT 1", seconds=0.2)]),  # real answer
        "ok": CaseResult("q", [_run(passed=True, sql="SELECT 1")]),
        "edge": CaseResult("q", [_run(seconds=5.0)]),  # exactly the limit: kept
        "mixed": CaseResult("q", [_run(passed=True, sql="S"), _run(seconds=1.0)]),
    }
    return report


def test_select_replay_picks_failed_no_sql_runs_under_the_limit() -> None:
    assert select_replay(_report(), 5.0) == [("infra", 0), ("mixed", 1)]
    assert select_replay(_report(), 6.0) == [("infra", 0), ("edge", 0), ("mixed", 1)]


def test_the_rule_is_independent_of_the_engine() -> None:
    a, b = _report("a"), _report("b")
    b.meta["backend_url"] = "http://other"
    assert select_replay(a) == select_replay(b)


def test_apply_replay_replaces_the_slot_and_keeps_the_original() -> None:
    report = _report()
    fresh = [
        _run(passed=True, sql="SELECT 2", seconds=12.0),
        _run(sql="S2", seconds=9.0),
    ]
    out = apply_replay(report, select_replay(report), fresh, max_seconds=5.0)
    assert out.cases["infra"].runs[0].sql == "SELECT 2"
    assert out.cases["mixed"].runs[1].sql == "S2"
    assert out.cases["mixed"].runs[0].passed  # the other slot is untouched
    replaced = out.meta["replay"]["replaced"]
    assert [(r["case"], r["run"]) for r in replaced] == [("infra", 0), ("mixed", 1)]
    assert replaced[0]["original"]["sql"] is None
    # the input report is not modified
    assert report.cases["infra"].runs[0].sql is None
    assert "replay" not in report.meta


def test_a_second_replay_keeps_the_earlier_originals() -> None:
    first = apply_replay(_report(), [("infra", 0)], [_run(seconds=0.5)], max_seconds=5)
    second = apply_replay(first, [("infra", 0)], [_run(sql="Z")], max_seconds=5)
    assert len(second.meta["replay"]["replaced"]) == 2


def test_replay_report_runs_only_the_selected_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Job] = []

    def fake_jobs(jobs: Any, url: str, **kw: Any) -> list[Run]:
        seen.extend(jobs)
        return [_run(passed=True, sql="NEW", seconds=20.0) for _ in jobs]

    monkeypatch.setattr(replay_mod, "run_jobs", fake_jobs)
    cases = [_case(c, "dbx") for c in ("infra", "slow", "wrong", "ok", "edge", "mixed")]
    out, n = replay_report(_report(), cases, "http://x", target_db=None)
    assert n == 2 and [(j.case.id, j.slot, j.target_db) for j in seen] == [
        ("infra", 0, "dbx"),
        ("mixed", 1, "dbx"),
    ]
    assert out.cases["infra"].runs[0].sql == "NEW"
    assert out.cases["wrong"].runs[0].sql == "SELECT 1"


def test_replay_with_nothing_to_do_changes_nothing() -> None:
    report = Report(
        meta={"label": "x"}, cases={"a": CaseResult("q", [_run(True, "S")])}
    )
    out, n = replay_report(report, [_case("a")], "http://x")
    assert n == 0 and out is report


def test_replay_rejects_a_case_missing_from_the_files() -> None:
    with pytest.raises(ValueError, match="infra"):
        replay_report(_report(), [_case("other")], "http://x")


def test_cli_replay_writes_a_copy_next_to_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(cli, "preflight", lambda *a, **k: {"backend_url": "u"})
    monkeypatch.setattr(
        replay_mod,
        "run_jobs",
        lambda jobs, url, **k: [_run(True, "N", 9.0) for _ in jobs],
    )
    cases = tmp_path / "c.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": cid,
                        "question": "q",
                        "gold": [{"label": "g", "sql": "S", "expected": [[1]]}],
                    }
                    for cid in ("infra", "slow", "wrong", "ok", "edge", "mixed")
                ]
            }
        )
    )
    src = tmp_path / "main.json"
    _report().save(src)
    before = src.read_text()
    assert cli.main(["replay", str(src), "--cases", str(cases)]) == 0
    assert src.read_text() == before  # the original is untouched
    out = Report.load(tmp_path / "main.replayed.json")
    assert out.cases["infra"].runs[0].sql == "N"
    assert len(out.meta["replay"]["replaced"]) == 2


# ----------------------------------------------------------------- parallel runs


def test_clamp_workers() -> None:
    assert clamp_workers(0) == 1 and clamp_workers(-3) == 1
    assert clamp_workers(4) == 4
    assert clamp_workers(500) == MAX_WORKERS


def _fake_ask(log: list[tuple[str, str | None]], gauge: dict[str, int]):
    lock = threading.Lock()

    def ask(client: Any, url: str, question: str, **kw: Any) -> Answer:
        with lock:
            gauge["now"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["now"])
            log.append((question, kw.get("target_db")))
        time.sleep(0.05)
        with lock:
            gauge["now"] -= 1
        return Answer(sql=f"SQL for {question}", rows=[[1]], seconds=0.05)

    return ask


def test_run_jobs_is_concurrent_but_returns_results_in_job_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[str, str | None]] = []
    gauge = {"now": 0, "peak": 0}
    monkeypatch.setattr(runner, "ask", _fake_ask(log, gauge))
    jobs = [Job(_case(f"c{i}"), 0) for i in range(12)]
    runs = run_jobs(jobs, "http://x", workers=4)
    assert [r.sql for r in runs] == [f"SQL for question c{i}" for i in range(12)]
    assert 1 < gauge["peak"] <= 4


def test_run_jobs_never_exceeds_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    gauge = {"now": 0, "peak": 0}
    monkeypatch.setattr(runner, "ask", _fake_ask([], gauge))
    run_jobs([Job(_case(f"c{i}"), 0) for i in range(30)], "http://x", workers=100)
    assert gauge["peak"] <= MAX_WORKERS


def test_run_cases_keeps_repeat_order_and_routes_each_case_to_its_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[str, str | None]] = []
    monkeypatch.setattr(runner, "ask", _fake_ask(log, {"now": 0, "peak": 0}))
    cases = [_case("a", "db_a"), _case("b", "db_b"), _case("c")]
    report = runner.run_cases(cases, "http://x", label="l", repeats=2, workers=3)
    assert list(report.cases) == ["a", "b", "c"]
    assert all(len(r.runs) == 2 for r in report.cases.values())
    assert sorted(set(log)) == [
        ("question a", "db_a"),
        ("question b", "db_b"),
        ("question c", None),
    ]
    assert report.meta["workers"] == 3
    log.clear()
    runner.run_cases(cases, "http://x", label="l", repeats=1, target_db="forced")
    assert {db for _, db in log} == {"forced"}  # an explicit --target-db wins
