# SPDX-License-Identifier: Apache-2.0

"""Drive a running backend with each question and score what comes back.

The runner talks to ``POST /api/chat/completions`` exactly as the frontend does,
without a ``conversation_id`` so every question is a fresh, stateless turn (no
history leaks between questions and no user identity is needed). It scores the
*executed rows* of the ``result`` event against the case's accepted answers; it
never looks at the SQL text.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator

import httpx

from dev_tools.evals.cases import Case
from dev_tools.evals.compare import compare_any
from dev_tools.evals.report import CaseResult, Report, Run, new_meta

CHAT_PATH = "/api/chat/completions"
# The pipeline retries SQL several times and each step is an LLM call, so a
# single question can legitimately take minutes.
DEFAULT_TIMEOUT_S = 300.0
# Enough to re-score any case here; a report must not balloon on a wide result.
MAX_STORED_ROWS = 50


@dataclass
class Answer:
    """What one question produced, reduced to what scoring needs."""

    sql: str | None = None
    rows: list[list[Any]] | None = None
    text: str | None = None
    error: str | None = None
    seconds: float = 0.0


def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yield the JSON payload of each ``data:`` frame until ``[DONE]``."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        body = line[len("data:") :].strip()
        if body == "[DONE]":
            return
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def rows_from_result(answer: dict[str, Any]) -> list[list[Any]] | None:
    """Pull the executed rows out of a ``result`` answer, whatever their packaging.

    ``sql_response_from_db`` is a list holding either row dicts or JSON strings
    of a row-dict list (what ``DataFrame.to_json`` produces). ``None`` means no
    SQL ran, which is a different outcome from a query that returned zero rows.
    """
    raw = answer.get("sql_response_from_db")
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else [raw]
    rows: list[list[Any]] = []
    for item in items:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                return None
        if isinstance(item, dict):
            rows.append(list(item.values()))
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, dict):
                    rows.append(list(sub.values()))
                elif isinstance(sub, list):
                    rows.append(list(sub))
                else:
                    rows.append([sub])
        else:
            rows.append([item])
    return rows


def answer_from_events(events: Iterable[dict[str, Any]]) -> Answer:
    """Fold a stream of events into one ``Answer``; the newest ``result`` wins."""
    out = Answer()
    for event in events:
        kind = event.get("type")
        if kind == "sql":
            out.sql = event.get("sql") or out.sql
        elif kind == "result":
            payload = event.get("answer") or {}
            out.sql = payload.get("sql_code") or out.sql
            out.text = payload.get("response")
            out.rows = rows_from_result(payload)
        elif kind == "error":
            out.error = event.get("message") or "error"
    return out


def ask(
    client: httpx.Client,
    base_url: str,
    question: str,
    *,
    target_db: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Answer:
    """Send one question and fold the stream into an ``Answer``."""
    body: dict[str, Any] = {"question": question}
    if target_db:
        body["target_db"] = target_db
    started = time.perf_counter()
    try:
        with client.stream(
            "POST", base_url.rstrip("/") + CHAT_PATH, json=body, timeout=timeout
        ) as response:
            if response.status_code != 200:
                detail = response.read().decode("utf-8", errors="replace")[:300]
                return Answer(
                    error=f"HTTP {response.status_code}: {detail}",
                    seconds=time.perf_counter() - started,
                )
            answer = answer_from_events(parse_sse(response.iter_lines()))
    except httpx.HTTPError as exc:
        return Answer(
            error=f"{type(exc).__name__}: {exc}", seconds=time.perf_counter() - started
        )
    answer.seconds = time.perf_counter() - started
    return answer


def score(case: Case, answer: Answer) -> Run:
    """Score one answer against a case's accepted results."""
    if answer.error and answer.rows is None:
        return Run(
            False, "error", sql=answer.sql, seconds=answer.seconds, error=answer.error
        )
    verdict = compare_any(
        answer.rows,
        case.accepted(),
        ordered=case.ordered,
        allow_extra_columns=case.allow_extra_columns,
        allow_missing_columns=case.allow_missing_columns,
    )
    return Run(
        passed=verdict.passed,
        reason=verdict.reason,
        matched=verdict.matched,
        sql=answer.sql,
        rows=None if answer.rows is None else len(answer.rows),
        seconds=round(answer.seconds, 2),
        error=answer.error,
        actual=None if answer.rows is None else answer.rows[:MAX_STORED_ROWS],
    )


def run_cases(
    cases: list[Case],
    base_url: str,
    *,
    label: str,
    repeats: int = 3,
    target_db: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    on_run: Callable[[Case, int, Run], None] | None = None,
) -> Report:
    """Run every case ``repeats`` times against *base_url* and report."""
    report = Report(
        meta=new_meta(label, base_url=base_url, repeats=repeats, target_db=target_db)
    )
    with httpx.Client() as client:
        for case in cases:
            result = CaseResult(case.question)
            for i in range(repeats):
                run = score(
                    case,
                    ask(
                        client,
                        base_url,
                        case.question,
                        target_db=target_db,
                        timeout=timeout,
                    ),
                )
                result.runs.append(run)
                if on_run:
                    on_run(case, i, run)
            report.cases[case.id] = result
    return report


def rescore(report: Report, cases: list[Case], *, strict: bool = False) -> Report:
    """Re-score a saved report with the stored rows, without running anything.

    ``strict`` ignores every per-case tolerance (extra or missing columns), so
    one set of runs can be reported under both conventions. Runs that were
    saved without their rows cannot be re-scored and raise.
    """
    by_id = {c.id: c for c in cases}
    out = Report(
        meta={**report.meta, "rescored": "strict" if strict else "as-declared"}
    )
    for cid, result in report.cases.items():
        case = by_id.get(cid)
        if case is None:
            raise ValueError(f"{cid}: not in the cases file")
        if strict:
            case = replace(case, allow_extra_columns=False, allow_missing_columns=False)
        runs: list[Run] = []
        for run in result.runs:
            if run.error and run.actual is None:
                runs.append(run)  # the run itself failed; nothing to re-score
                continue
            if run.actual is None and run.rows is not None:
                raise ValueError(f"{cid}: report was saved without the rows")
            answer = Answer(
                sql=run.sql, rows=run.actual, error=run.error, seconds=run.seconds
            )
            runs.append(score(case, answer))
        out.cases[cid] = CaseResult(result.question, runs)
    return out
