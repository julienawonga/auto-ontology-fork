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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator, Sequence

import httpx

from auto_ontology.server.internal_auth import INTERNAL_TOKEN_HEADER
from dev_tools.evals.cases import Case
from dev_tools.evals.compare import compare_any
from dev_tools.evals.report import CaseResult, Report, Run, new_meta

CHAT_PATH = "/api/chat/completions"
# The pipeline retries SQL several times and each step is an LLM call, so a
# single question can legitimately take minutes.
DEFAULT_TIMEOUT_S = 300.0
# The backend answers 503 + Retry-After when every chat slot (its
# CHAT_MAX_CONCURRENT) is busy for CHAT_WAIT_S. That is back-pressure, not a
# failed question: wait as told and try again, within these bounds.
MAX_BUSY_RETRIES = 10
MAX_BUSY_WAIT_S = 900.0
DEFAULT_RETRY_AFTER_S = 5.0
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
    # Seconds spent waiting out 503 "all chat slots busy" answers; excluded
    # from ``seconds`` so latency stays the time the question actually took.
    busy_waits: list[float] = field(default_factory=list)
    # The answer's ``knowledge_trace`` (what business knowledge generation was
    # given and which of it the SQL appears to use); None from an older backend.
    knowledge_trace: list[dict[str, Any]] | None = None
    # The answer's ``question_language`` / ``question_english`` (only present
    # when QUESTION_TRANSLATION_ENABLED ran); None otherwise.
    question_language: str | None = None
    question_english: str | None = None


def retry_after_s(response: httpx.Response) -> float:
    """The ``Retry-After`` delay in seconds (clamped 1..60), or the default."""
    try:
        value = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return max(1.0, min(value, 60.0))


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
            trace = payload.get("knowledge_trace")
            out.knowledge_trace = trace if isinstance(trace, list) else None
            language = payload.get("question_language")
            english = payload.get("question_english")
            out.question_language = language if isinstance(language, str) else None
            out.question_english = english if isinstance(english, str) else None
        elif kind == "error":
            out.error = event.get("message") or "error"
    return out


def ask(
    client: httpx.Client,
    base_url: str,
    question: str,
    *,
    target_db: str | None = None,
    evidence: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
    max_busy_retries: int = MAX_BUSY_RETRIES,
    max_busy_wait_s: float = MAX_BUSY_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Answer:
    """Send one question and fold the stream into an ``Answer``.

    A 503 (every backend chat slot busy) is retried after the ``Retry-After``
    the backend sent, at most *max_busy_retries* times and *max_busy_wait_s*
    seconds in total; the waits are recorded in ``Answer.busy_waits``.
    """
    body: dict[str, Any] = {"question": question}
    if target_db:
        body["target_db"] = target_db
    if evidence:
        body["evidence"] = evidence
    waits: list[float] = []
    started = time.perf_counter()

    def elapsed() -> float:
        return max(0.0, time.perf_counter() - started - sum(waits))

    while True:
        try:
            with client.stream(
                "POST", base_url.rstrip("/") + CHAT_PATH, json=body, timeout=timeout
            ) as response:
                if response.status_code == 503:
                    delay = retry_after_s(response)
                    if len(waits) < max_busy_retries and (
                        sum(waits) + delay <= max_busy_wait_s
                    ):
                        response.read()
                        waits.append(delay)
                        sleep(delay)
                        continue
                if response.status_code != 200:
                    detail = response.read().decode("utf-8", errors="replace")[:300]
                    return Answer(
                        error=f"HTTP {response.status_code}: {detail}",
                        seconds=elapsed(),
                        busy_waits=waits,
                    )
                answer = answer_from_events(parse_sse(response.iter_lines()))
        except httpx.HTTPError as exc:
            return Answer(
                error=f"{type(exc).__name__}: {exc}",
                seconds=elapsed(),
                busy_waits=waits,
            )
        answer.seconds = elapsed()
        answer.busy_waits = waits
        return answer


def score(case: Case, answer: Answer) -> Run:
    """Score one answer against a case's accepted results."""
    if answer.error and answer.rows is None:
        return Run(
            False,
            "error",
            sql=answer.sql,
            seconds=answer.seconds,
            error=answer.error,
            knowledge_trace=answer.knowledge_trace,
            question_language=answer.question_language,
            question_english=answer.question_english,
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
        knowledge_trace=answer.knowledge_trace,
        question_language=answer.question_language,
        question_english=answer.question_english,
    )


# More concurrent chat requests than this exhaust the backend's database
# connections on one local stack and fail questions instantly (observed at ~6).
MAX_WORKERS = 6


def clamp_workers(requested: int) -> int:
    """The worker count actually used: at least 1, at most ``MAX_WORKERS``."""
    return max(1, min(requested, MAX_WORKERS))


@dataclass(frozen=True)
class Job:
    """One run of one case; ``slot`` says which repeat it is."""

    case: Case
    slot: int
    target_db: str | None = None


def run_jobs(
    jobs: Sequence[Job],
    base_url: str,
    *,
    workers: int = 1,
    timeout: float = DEFAULT_TIMEOUT_S,
    token: str | None = None,
    on_run: Callable[[Case, int, Run], None] | None = None,
    busy_waits: list[float] | None = None,
) -> list[Run]:
    """Run every job, up to *workers* at a time; results are in job order.

    A job that dies of an unexpected (non-HTTP) exception -- a case without a
    snapshot, a bug in scoring -- is recorded as that case's error instead of
    aborting the whole run. *token* is the backend's internal token, sent as a
    header on every request and never written to a report.
    """
    lock = threading.Lock()
    headers = {INTERNAL_TOKEN_HEADER: token} if token else {}
    with httpx.Client(headers=headers) as client:

        def one(job: Job) -> Run:
            try:
                answer = ask(
                    client,
                    base_url,
                    job.case.question,
                    target_db=job.target_db,
                    evidence=job.case.evidence,
                    timeout=timeout,
                )
                if busy_waits is not None and answer.busy_waits:
                    with lock:
                        busy_waits.extend(answer.busy_waits)
                run = score(job.case, answer)
            except Exception as exc:  # one bad case must not abort the run
                run = Run(
                    passed=False,
                    reason="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if on_run:
                with lock:
                    on_run(job.case, job.slot, run)
            return run

        n = clamp_workers(workers)
        if n == 1:
            return [one(job) for job in jobs]
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(one, jobs))


def effective_target_db(case: Case, target_db: str | None) -> str | None:
    """An explicit ``--target-db`` wins; otherwise the database the case names."""
    return target_db or case.target_db or None


def run_cases(
    cases: list[Case],
    base_url: str,
    *,
    label: str,
    repeats: int = 3,
    target_db: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    token: str | None = None,
    on_run: Callable[[Case, int, Run], None] | None = None,
    provenance: dict[str, Any] | None = None,
    workers: int = 1,
) -> Report:
    """Run every case ``repeats`` times against *base_url* and report.

    *provenance* (index state, cases hash, ...) is stored in the report meta.
    With *workers* above 1 the runs go out concurrently (capped at
    ``MAX_WORKERS``); each case keeps its runs in repeat order. *token* is the
    backend's ``INTERNAL_API_TOKEN``: this talks to the API directly, not
    through the web app, so it must present it itself when the backend
    requires one.
    """
    report = Report(
        meta=new_meta(
            label,
            repeats=repeats,
            target_db=target_db,
            workers=clamp_workers(workers),
            **{"backend_url": base_url, **(provenance or {})},
        )
    )
    jobs = [
        Job(case, i, effective_target_db(case, target_db))
        for case in cases
        for i in range(repeats)
    ]
    busy_waits: list[float] = []
    runs = run_jobs(
        jobs,
        base_url,
        workers=workers,
        timeout=timeout,
        token=token,
        on_run=on_run,
        busy_waits=busy_waits,
    )
    # 503 "all slots busy" back-pressure the runs sat out (see ``ask``).
    report.meta["busy_retries"] = len(busy_waits)
    report.meta["busy_wait_s"] = round(sum(busy_waits), 1)
    for case in cases:
        report.cases[case.id] = CaseResult(case.question)
    for job, run in zip(jobs, runs):
        report.cases[job.case.id].runs.append(run)
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
                sql=run.sql,
                rows=run.actual,
                error=run.error,
                seconds=run.seconds,
                knowledge_trace=run.knowledge_trace,
                question_language=run.question_language,
                question_english=run.question_english,
            )
            runs.append(score(case, answer))
        out.cases[cid] = CaseResult(result.question, runs)
    return out
