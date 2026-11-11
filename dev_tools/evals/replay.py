# SPDX-License-Identifier: Apache-2.0

"""Re-run the runs that failed because of the infrastructure, not the engine.

Under load the backend can fail a question instantly (an exhausted connection
pool, a refused connection): an **error**, no SQL, a few milliseconds. That says
nothing about the engine. One rule picks those runs, the same for every engine:
a run that **errored** (``error`` set, or reason ``"error"``), **produced no
SQL** and took **under ``max_seconds``**. They are run again and replace their
slot in a copy of the report; the runs they replace stay in the report's
``meta.replay`` so nothing is lost. A run that produced SQL, that recorded no
error, or that took longer, is never touched, however it scored.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Sequence

from dev_tools.evals.cases import Case
from dev_tools.evals.report import CaseResult, Report, Run
from dev_tools.evals.runner import Job, effective_target_db, run_jobs

DEFAULT_MAX_SECONDS = 5.0


def is_infrastructure_failure(run: Run, max_seconds: float) -> bool:
    """An errored run with no SQL in under *max_seconds*: infrastructure, not an answer."""
    errored = run.error is not None or run.reason == "error"
    return errored and not run.passed and not run.sql and run.seconds < max_seconds


def select_replay(
    report: Report, max_seconds: float = DEFAULT_MAX_SECONDS
) -> list[tuple[str, int]]:
    """``(case id, run index)`` of every run the rule selects, in report order."""
    return [
        (cid, i)
        for cid, result in report.cases.items()
        for i, run in enumerate(result.runs)
        if is_infrastructure_failure(run, max_seconds)
    ]


def apply_replay(
    report: Report,
    selected: Sequence[tuple[str, int]],
    fresh: Sequence[Run],
    *,
    max_seconds: float,
) -> Report:
    """A copy of *report* with the selected runs replaced by *fresh* ones."""
    cases = {
        cid: CaseResult(result.question, list(result.runs))
        for cid, result in report.cases.items()
    }
    history = list(report.meta.get("replay", {}).get("replaced", []))
    for (cid, i), run in zip(selected, fresh):
        history.append({"case": cid, "run": i, "original": asdict(cases[cid].runs[i])})
        cases[cid].runs[i] = run
    meta = {
        **report.meta,
        "replay": {
            "rule": f"errored, no SQL, under {max_seconds:g}s",
            "replaced": history,
        },
    }
    return Report(meta=meta, cases=cases)


def replay_report(
    report: Report,
    cases: Sequence[Case],
    base_url: str,
    *,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    target_db: str | None = None,
    workers: int = 1,
    timeout: float | None = None,
    on_run: Callable[[Case, int, Run], None] | None = None,
) -> tuple[Report, int]:
    """Replay the selected runs; returns the new report and how many were re-run."""
    by_id = {c.id: c for c in cases}
    selected = select_replay(report, max_seconds)
    unknown = sorted({cid for cid, _ in selected if cid not in by_id})
    if unknown:
        raise ValueError(f"not in the cases files: {', '.join(unknown)}")
    if not selected:
        return report, 0
    jobs = [
        Job(by_id[cid], i, effective_target_db(by_id[cid], target_db))
        for cid, i in selected
    ]
    kwargs: dict[str, Any] = {"workers": workers, "on_run": on_run}
    if timeout is not None:
        kwargs["timeout"] = timeout
    fresh = run_jobs(jobs, base_url, **kwargs)
    return apply_replay(report, selected, fresh, max_seconds=max_seconds), len(fresh)


__all__ = [
    "DEFAULT_MAX_SECONDS",
    "apply_replay",
    "is_infrastructure_failure",
    "replay_report",
    "select_replay",
]
