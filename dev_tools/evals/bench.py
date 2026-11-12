# SPDX-License-Identifier: Apache-2.0

"""One-command evaluation bench: split once, run a slice, score against a baseline.

python -m dev_tools.evals bench split --cases CASES --out SPLIT --seed 7 --dev 0.3 --canary 0.1
python -m dev_tools.evals bench run   --split SPLIT --slice dev --label main --url ...
python -m dev_tools.evals bench score --run RUN [--baseline RUN] --dsn-template ...

What it builds in, because each was learned the hard way:

* **One process.** Every database of a slice runs inside one Python process;
  nothing is fetched per database, so a transient package-index or DNS failure
  cannot kill half a run.
* **The backend's chat cap is respected.** Workers are ``min(requested, cap from
  /api/health, 6)``: past the cap the gateway blocks the IP.
* **Infrastructure failures are not engine failures.** After the run, the runs
  the replay rule selects (errored, no SQL, under N seconds) are run once more,
  the same rule for every engine. The ones still failing are excluded from the
  paired test for both arms, and counted.
* **A single run misleads.** Two runs of one engine on 180 LiveSQLBench tasks
  differ by about 4 points. Decisions use a paired comparison with the exact
  McNemar test, and the observed noise (repeats or a sibling run) is printed.
* **The official metric is authoritative for LiveSQLBench** (``lsb-score``); the
  harness comparison is only the fallback for other cases.
* **Held-out discipline.** ``split`` freezes dev / heldout / canary sets; develop
  on dev, decide on heldout, replay the canary before promoting knowledge changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from auto_ontology.server.internal_auth import INTERNAL_TOKEN_ENV, INTERNAL_TOKEN_HEADER
from dev_tools.evals.cases import Case, load_cases
from dev_tools.evals.lsb import (
    ID_PREFIX,
    MODES,
    OFFICIAL,
    OFFICIAL_FETCH_CAP,
    SOURCE,
    TIER_TAG,
    Outcome,
    OfficialScore,
    PostgresExecutor,
    case_tier,
    merge_scores,
    paired_counts,
    score_report,
)
from dev_tools.evals.preflight import PreflightError, preflight
from dev_tools.evals.replay import (
    DEFAULT_MAX_SECONDS,
    is_infrastructure_failure,
    replay_report,
    select_replay,
)
from dev_tools.evals.report import CaseResult, Report, Run, merge_reports, new_meta
from dev_tools.evals.runner import MAX_WORKERS, Job, run_jobs
from dev_tools.integrity.freeze import check_frozen, file_cases_sha256, freeze

REPO_ROOT = Path(__file__).resolve().parents[2]
SLICES = ("dev", "heldout", "canary")
SPLIT_FILE = "split.json"
FROZEN_FILE = "FROZEN.sha256"
MANIFEST = "manifest.json"
ALPHA = 0.05
IMPROVED = "IMPROVED"
REGRESSED = "REGRESSED"
NO_CHANGE = "NO SIGNIFICANT CHANGE"

SLICE_NOTES = {
    "dev": (
        "dev slice: this is the set you develop on, so it is NOT evidence. "
        "Decide on the heldout slice."
    ),
    "heldout": (
        "heldout slice: every look at it spends it. Decide once; do not tune on "
        "what you saw here."
    ),
    "canary": (
        "canary slice: a small regression guard to replay before promoting "
        "knowledge changes; too small to prove an improvement."
    ),
}
FUTURE_WORK = "Paraphrased questions (a memorisation check) are future work."


class BenchError(Exception):
    """A refusal with a message for the operator (exit code 2)."""


def _inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == REPO_ROOT or REPO_ROOT in resolved.parents


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------ split


def is_lsb_case(raw_file: dict[str, Any], raw_case: dict[str, Any]) -> bool:
    """A LiveSQLBench case: its file says so, or its id or tags do."""
    return (
        raw_file.get("source") == SOURCE
        or str(raw_case.get("id", "")).startswith(ID_PREFIX)
        or any(str(t).startswith(TIER_TAG) for t in raw_case.get("tags", ()))
    )


def _stratum(raw_case: dict[str, Any], db: str) -> tuple[str, str]:
    tier = case_tier(
        Case(raw_case["id"], "", (), tags=tuple(raw_case.get("tags", ())))  # type: ignore[arg-type]
    )
    return db, tier


def _take(n: int, share: float, carry: float) -> tuple[int, float]:
    """How many of *n* go to a slice of *share*, carrying the rounding forward.

    The carry keeps the global proportions right when many strata are tiny.
    """
    want = n * share + carry
    taken = max(0, min(n, math.floor(want + 0.5)))
    return taken, want - taken


def assign_slices(
    strata: dict[tuple[str, str], list[str]], seed: int, dev: float, canary: float
) -> dict[str, str]:
    """Case id -> slice, deterministic for a seed, stratified by (database, tier)."""
    out: dict[str, str] = {}
    carry_c = carry_d = 0.0
    for key in sorted(strata):
        ids = sorted(strata[key], key=lambda i: (_sha(f"{seed}:{i}"), i))
        n_c, carry_c = _take(len(ids), canary, carry_c)
        n_d, carry_d = _take(len(ids) - n_c, dev / (1 - canary), carry_d)
        for i, cid in enumerate(ids):
            out[cid] = "canary" if i < n_c else "dev" if i < n_c + n_d else "heldout"
    return out


def split_cases(
    cases_dir: Path, out: Path, *, seed: int, dev: float, canary: float
) -> dict[str, Any]:
    """Split a directory of case files into frozen dev / heldout / canary sets."""
    if not (0 < dev < 1 and 0 <= canary < 1 and dev + canary < 1):
        raise BenchError("need 0 < --dev < 1, 0 <= --canary < 1 and dev + canary < 1")
    files = sorted(cases_dir.glob("*.json")) if cases_dir.is_dir() else []
    if not files:
        raise BenchError(f"no *.json case files in {cases_dir}")
    if (out / SPLIT_FILE).exists() or any((out / s).exists() for s in SLICES):
        raise BenchError(
            f"{out} already holds a split; refusing to overwrite it (a split is "
            "made once, so the heldout set stays unseen). Use a new --out."
        )
    raws: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    seen: set[str] = set()
    lsb = False
    for path in files:
        raw_file = json.loads(path.read_text(encoding="utf-8"))
        file_db = str(raw_file.get("database") or path.stem)
        for raw_case in raw_file["cases"]:
            if raw_case["id"] in seen:
                raise BenchError(f"duplicate case id {raw_case['id']!r}")
            seen.add(raw_case["id"])
            lsb = lsb or is_lsb_case(raw_file, raw_case)
            db = str(raw_case.get("database") or file_db)
            raws.append((raw_file, db, raw_case))
    if lsb and _inside_repo(out):
        raise BenchError(
            "--out is inside this repository and the cases are LiveSQLBench's: "
            "their gold SQL must stay outside it (no-redistribution request)."
        )
    strata: dict[tuple[str, str], list[str]] = {}
    for _, db, raw_case in raws:
        strata.setdefault(_stratum(raw_case, db), []).append(raw_case["id"])
    where = assign_slices(strata, seed, dev, canary)

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {s: {} for s in SLICES}
    meta_by_db: dict[str, dict[str, Any]] = {}
    for raw_file, db, raw_case in raws:
        case = {**raw_case, "database": db}
        grouped[where[raw_case["id"]]].setdefault(db, []).append(case)
        meta_by_db.setdefault(
            db,
            {
                k: v
                for k, v in raw_file.items()
                if k not in ("cases", "frozen_at", "frozen_sha256", "database")
            },
        )
    counts: dict[str, int] = {}
    for name in SLICES:
        slice_dir = out / name
        slice_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        counts[name] = 0
        for db in sorted(grouped[name]):
            path = slice_dir / f"{db}.json"
            payload = {**meta_by_db[db], "database": db, "cases": grouped[name][db]}
            path.write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))
            lines.append(f"{freeze(path)}  {path.name}")
            counts[name] += len(grouped[name][db])
        (slice_dir / FROZEN_FILE).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    info = {
        "seed": seed,
        "dev": dev,
        "canary": canary,
        "stratified_by": ["database", "difficulty tier"],
        "counts": counts,
        "lsb": lsb,
        "split_id": split_id(out),
    }
    (out / SPLIT_FILE).write_bytes((json.dumps(info, indent=2) + "\n").encode("utf-8"))
    return info


def split_id(split_dir: Path) -> str:
    """Identity of a split: a hash over the three FROZEN.sha256 files."""
    parts = [(split_dir / s / FROZEN_FILE).read_text(encoding="utf-8") for s in SLICES]
    return _sha("\x00".join(parts))[:16]


def verify_slice(split_dir: Path, slice_name: str) -> list[Path]:
    """The slice's case files, after checking them against FROZEN.sha256."""
    slice_dir = split_dir / slice_name
    listing = slice_dir / FROZEN_FILE
    if not listing.is_file():
        raise BenchError(f"{listing} not found: is {split_dir} a bench split?")
    expected: dict[str, str] = {}
    for line in listing.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            expected[name.strip()] = digest.strip()
    on_disk = {p.name: p for p in slice_dir.glob("*.json")}
    problems = [f"{n}: listed but missing" for n in expected if n not in on_disk]
    problems += [f"{n}: present but not listed" for n in on_disk if n not in expected]
    for name, path in on_disk.items():
        if name in expected and file_cases_sha256(path) != expected[name]:
            problems.append(f"{name}: cases differ from {FROZEN_FILE}")
        message = check_frozen(path)
        if message:
            problems.append(message)
    if problems:
        raise BenchError(
            f"the {slice_name} slice is not what was frozen:\n  "
            + "\n  ".join(problems)
        )
    return [on_disk[n] for n in sorted(expected)]


# -------------------------------------------------------------------------- run


def resolve_workers(requested: str, cap: int | None) -> tuple[int, str | None]:
    """Workers to use: ``min(requested, backend cap, 6)``; ``auto`` asks for the cap."""
    if requested != "auto":
        try:
            want = int(requested)
        except ValueError:
            raise BenchError("--workers is 'auto' or a positive integer") from None
        if want < 1:
            raise BenchError("--workers is 'auto' or a positive integer")
    else:
        want = cap if cap is not None else 2
    limits = [want, MAX_WORKERS] + ([cap] if cap is not None else [])
    used = min(limits)
    notes = []
    if cap is None:
        notes.append("the backend's chat cap is unknown (/api/health unreadable)")
    elif used < want:
        notes.append(
            f"--workers {requested} lowered to {used} (backend chat cap {cap})"
        )
    if want > MAX_WORKERS:
        notes.append(f"workers never exceed {MAX_WORKERS}")
    return used, "; ".join(notes) or None


_SHA_KEYS = ("git_sha", "git_commit", "commit", "build_sha", "revision")


def fetch_health(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        body = client.get(url.rstrip("/") + "/api/health", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def health_cap(health: dict[str, Any] | None) -> int | None:
    cap = ((health or {}).get("chat_workers") or {}).get("cap")
    return cap if isinstance(cap, int) and cap > 0 else None


def health_git_sha(health: dict[str, Any] | None) -> str | None:
    for source in (health or {}, (health or {}).get("build") or {}):
        if isinstance(source, dict):
            for key in _SHA_KEYS:
                if isinstance(source.get(key), str) and source[key]:
                    return str(source[key])
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "run"


def run_slice(
    split_dir: Path,
    slice_name: str,
    *,
    label: str,
    url: str,
    out_dir: Path,
    workers: str = "auto",
    repeats: int = 1,
    token: str | None = None,
    git_sha: str | None = None,
    notes: Sequence[str] = (),
    max_seconds: float = DEFAULT_MAX_SECONDS,
    allow_empty_index: bool = False,
    on_run: Callable[[Case, int, Run], None] | None = None,
) -> Path:
    """Run every database of a slice in this process, replay infra failures, merge."""
    if repeats < 1:
        raise BenchError("--repeats must be at least 1")
    files = verify_slice(split_dir, slice_name)
    info = json.loads((split_dir / SPLIT_FILE).read_text(encoding="utf-8"))
    if info.get("lsb") and _inside_repo(out_dir):
        raise BenchError(
            "--out-dir is inside this repository and the split is LiveSQLBench's: "
            "reports quote gold-derived rows and must stay outside it."
        )
    run_dir = out_dir / (
        f"{_slug(label)}-{slice_name}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    if run_dir.exists():
        raise BenchError(f"{run_dir} already exists")
    dbs: dict[str, list[Case]] = {}
    hashes: dict[str, str] = {}
    for path in files:
        db = str(
            json.loads(path.read_text(encoding="utf-8")).get("database") or path.stem
        )
        dbs[db] = load_cases(path)
        hashes[db] = file_cases_sha256(path)

    headers = {INTERNAL_TOKEN_HEADER: token} if token else {}
    started = _utc_now()
    with httpx.Client(headers=headers) as client:
        health = fetch_health(client, url)
        cap = health_cap(health)
        n_workers, worker_note = resolve_workers(workers, cap)
        if worker_note:
            print(f"workers: {n_workers} ({worker_note})", file=sys.stderr)
        provenance: dict[str, dict[str, Any]] = {}
        for db in dbs:
            try:
                provenance[db] = preflight(
                    url,
                    db,
                    allow_empty_index=allow_empty_index,
                    token=token,
                    client=client,
                )
            except PreflightError as exc:
                raise BenchError(str(exc)) from exc

    jobs = [
        Job(c, i, db)
        for db, cases in dbs.items()
        for c in cases
        for i in range(repeats)
    ]
    busy: list[float] = []
    runs = run_jobs(
        jobs, url, workers=n_workers, token=token, on_run=on_run, busy_waits=busy
    )
    reports: dict[str, Report] = {}
    for db, cases in dbs.items():
        report = Report(
            meta=new_meta(
                label,
                repeats=repeats,
                target_db=db,
                workers=n_workers,
                **{"backend_url": url, **provenance[db], "cases_sha256": hashes[db]},
            )
        )
        for case in cases:
            report.cases[case.id] = CaseResult(case.question)
        reports[db] = report
    for job, run in zip(jobs, runs):
        reports[job.target_db or ""].cases[job.case.id].runs.append(run)

    reports_dir = run_dir / "reports"
    infra_before = {db: len(select_replay(r, max_seconds)) for db, r in reports.items()}
    replayed: dict[str, Report] = {}
    for db, report in reports.items():
        report.save(reports_dir / f"{db}.json")
        replayed[db], _ = replay_report(
            report, dbs[db], url, max_seconds=max_seconds, target_db=db,
            workers=n_workers, token=token, on_run=on_run,
        )  # fmt: skip
        replayed[db].save(reports_dir / f"{db}.replayed.json")
    merge_reports(label, list(replayed.values())).save(run_dir / "merged.json")

    infra_after = {db: len(select_replay(r, max_seconds)) for db, r in replayed.items()}
    all_runs = [
        r for rep in replayed.values() for c in rep.cases.values() for r in c.runs
    ]
    manifest = {
        "bench": 1,
        "label": label,
        "slice": slice_name,
        "split_dir": str(split_dir.resolve()),
        "split_id": info.get("split_id") or split_id(split_dir),
        "lsb": bool(info.get("lsb")),
        "backend_url": url,
        "git_sha": git_sha or health_git_sha(health),
        "git_sha_source": "user"
        if git_sha
        else "endpoint"
        if health_git_sha(health)
        else "unknown",
        "started": started,
        "ended": _utc_now(),
        "repeats": repeats,
        "workers": {"requested": workers, "backend_cap": cap, "used": n_workers},
        "notes": list(notes),
        "databases": {
            db: {"questions": len(c), "cases_sha256": hashes[db]}
            for db, c in dbs.items()
        },
        "replay": {
            "rule": f"errored, no SQL, under {max_seconds:g}s",
            "max_seconds": max_seconds,
            "passes": 1,
            "infra_failures_before": sum(infra_before.values()),
            "infra_failures_after": sum(infra_after.values()),
        },
        "totals": {
            "runs": len(all_runs),
            "errored_runs": sum(1 for r in all_runs if r.error is not None),
            "busy_retries": len(busy),
            "busy_wait_s": round(sum(busy), 1),
        },
        "is_evidence": slice_name != "dev",
    }
    (run_dir / MANIFEST).write_bytes(
        (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    )
    return run_dir


# ------------------------------------------------------------------------ score

Scorer = Callable[[Report, list[Case]], OfficialScore]


@dataclass
class RunData:
    dir: Path
    manifest: dict[str, Any]
    reports: dict[str, Report]
    cases: dict[str, list[Case]]


@dataclass
class ScoredRun:
    data: RunData
    score: OfficialScore
    infra_unresolved: set[str] = field(default_factory=set)
    infra_runs: int = 0

    @property
    def label(self) -> str:
        return str(self.data.manifest["label"])


def load_run(run_dir: Path) -> RunData:
    manifest_path = run_dir / MANIFEST
    if not manifest_path.is_file():
        raise BenchError(f"{manifest_path} not found: is it a bench run directory?")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    split_dir = Path(manifest["split_dir"])
    files = {p.stem: p for p in verify_slice(split_dir, manifest["slice"])}
    reports: dict[str, Report] = {}
    cases: dict[str, list[Case]] = {}
    for db, path in files.items():
        recorded = manifest["databases"].get(db, {}).get("cases_sha256")
        if recorded != file_cases_sha256(path):
            raise BenchError(f"{db}: the split's cases changed since this run")
        report = run_dir / "reports" / f"{db}.replayed.json"
        if not report.is_file():
            raise BenchError(f"{report} not found")
        reports[db] = Report.load(report)
        cases[db] = load_cases(path)
    return RunData(run_dir, manifest, reports, cases)


def harness_score(report: Report, cases: Sequence[Case]) -> OfficialScore:
    """The harness verdict (its own row comparison), for cases that are not LiveSQLBench."""
    by_id = {c.id: c for c in cases}
    return OfficialScore(
        label=str(report.meta.get("label", "?")),
        outcomes={
            cid: [Outcome(r.passed, r.reason) for r in res.runs]
            for cid, res in report.cases.items()
        },
        tiers={cid: case_tier(by_id[cid]) for cid in report.cases if cid in by_id},
    )


def make_scorer(
    lsb: bool, dsn_template: str | None, mode: str, timeout_s: float
) -> tuple[Scorer, Callable[[], None]]:
    """The official LiveSQLBench metric for LSB cases, the harness one otherwise."""
    if not lsb:
        return (lambda report, cases: harness_score(report, cases)), (lambda: None)
    if not dsn_template or "{db}" not in dsn_template:
        raise BenchError(
            "--dsn-template with {db} is required to score LiveSQLBench cases"
        )
    executor = PostgresExecutor(
        dsn_template,
        timeout_s,
        fetch_cap=OFFICIAL_FETCH_CAP if mode == OFFICIAL else None,
    )
    gold_cache: dict[str, Any] = {}
    return (
        lambda report, cases: score_report(
            report, cases, executor, mode=mode, gold_cache=gold_cache
        ),
        executor.close,
    )


def score_run(data: RunData, scorer: Scorer) -> ScoredRun:
    max_seconds = float(
        data.manifest.get("replay", {}).get("max_seconds", DEFAULT_MAX_SECONDS)
    )
    per_db = [scorer(data.reports[db], data.cases[db]) for db in sorted(data.reports)]
    merged = merge_scores(per_db)
    if len(merged) != 1:
        raise BenchError("a run's reports must share one label")
    unresolved: set[str] = set()
    infra_runs = 0
    for db, report in data.reports.items():
        by_id = {c.id: c for c in data.cases[db]}
        for cid, res in report.cases.items():
            bad = [is_infrastructure_failure(r, max_seconds) for r in res.runs]
            infra_runs += sum(bad)
            if res.runs and all(bad) and cid in by_id:
                unresolved.add(cid)
    return ScoredRun(data, merged[0], unresolved, infra_runs)


def _without(score: OfficialScore, drop: set[str]) -> OfficialScore:
    return OfficialScore(
        score.label,
        {c: o for c, o in score.outcomes.items() if c not in drop},
        {c: t for c, t in score.tiers.items() if c not in drop},
        score.gold_problems,
    )


def within_run_discordance(score: OfficialScore) -> tuple[float, int] | None:
    """Mean share of repeat pairs of one question that disagree, and how many questions."""
    shares: list[float] = []
    for outcomes in score.outcomes.values():
        n = len(outcomes)
        if n < 2:
            continue
        pairs = n * (n - 1) // 2
        differ = sum(
            outcomes[i].passed != outcomes[j].passed
            for i in range(n)
            for j in range(i + 1, n)
        )
        shares.append(differ / pairs)
    return (sum(shares) / len(shares), len(shares)) if shares else None


@dataclass
class Verdict:
    verdict: str
    only_base: int
    only_cand: int
    both: int
    neither: int
    p_value: float
    paired: int
    excluded: int
    per_tier: dict[str, tuple[int, int, int, int]]
    noise_rate: float | None
    noise_source: str
    notes: list[str]


def decide(p_value: float, only_base: int, only_cand: int, alpha: float = ALPHA) -> str:
    """Significant and in one direction only; anything else is no change."""
    if p_value < alpha and only_cand != only_base:
        return IMPROVED if only_cand > only_base else REGRESSED
    return NO_CHANGE


def compare_scored(
    base: ScoredRun,
    cand: ScoredRun,
    noise_run: ScoredRun | None = None,
    *,
    alpha: float = ALPHA,
) -> Verdict:
    """Paired exact McNemar of a candidate against a baseline, with the noise band."""
    notes: list[str] = []
    for key, what in (("slice", "slice"), ("split_id", "split")):
        if base.data.manifest.get(key) != cand.data.manifest.get(key):
            raise BenchError(
                f"the runs used a different {what}; they are not comparable"
            )
    drop = base.infra_unresolved | cand.infra_unresolved
    fa, fb = _without(base.score, drop), _without(cand.score, drop)
    if fa.outcomes.keys() != fb.outcomes.keys():
        raise BenchError("the runs do not hold the same questions")
    paired = paired_counts(fa, fb)
    if drop:
        notes.append(
            f"{len(drop)} question(s) excluded from the test for both arms: every run "
            "of one of them is an unreplayed infrastructure failure"
        )
    per_tier: dict[str, tuple[int, int, int, int]] = {}
    for tier in sorted({*fa.tiers.values()}):
        ids = {c for c, t in fa.tiers.items() if t == tier}
        p = paired_counts(
            OfficialScore("a", {c: fa.outcomes[c] for c in ids}, fa.tiers),
            OfficialScore("b", {c: fb.outcomes[c] for c in ids}, fb.tiers),
        )
        per_tier[tier] = (p.both, p.only_a, p.only_b, p.neither)
    rate: float | None = None
    source = ""
    within = within_run_discordance(fa)
    if noise_run is not None:
        other = _without(noise_run.score, drop)
        shared = fa.outcomes.keys() & other.outcomes.keys()
        if shared:
            p = paired_counts(
                OfficialScore("a", {c: fa.outcomes[c] for c in shared}, fa.tiers),
                OfficialScore("n", {c: other.outcomes[c] for c in shared}, fa.tiers),
            )
            rate = (p.only_a + p.only_b) / len(shared)
            source = f"sibling noise run {noise_run.data.dir.name}"
    elif within is not None:
        rate = within[0]
        source = f"{base.data.manifest.get('repeats')} repeats of the baseline"
    if rate is None:
        notes.append(
            "no noise estimate (single-run baseline, no --noise run): two runs of "
            "the same engine can differ by about 4 points on 180 LiveSQLBench tasks"
        )
    verdict = decide(paired.p_value, paired.only_a, paired.only_b, alpha)
    slice_name = str(cand.data.manifest["slice"])
    notes.append(SLICE_NOTES[slice_name])
    if verdict != NO_CHANGE and slice_name == "dev":
        notes.append("not evidence of a real change; confirm on the heldout slice")
    return Verdict(
        verdict, paired.only_a, paired.only_b, paired.both, paired.neither,
        paired.p_value, len(fa.outcomes), len(drop), per_tier, rate, source, notes,
    )  # fmt: skip


def render_run(scored: ScoredRun) -> str:
    m = scored.data.manifest
    lines = [
        f"{scored.label}  [{m['slice']} slice, {m['repeats']} repeat(s), "
        f"{'official LiveSQLBench metric' if m.get('lsb') else 'harness comparison'}]"
    ]
    for tier, (n, rate) in scored.score.tier_scores().items():
        lines.append(f"  {tier:<12} {rate:6.1%}  ({n} questions)")
    rep = m.get("replay", {})
    lines.append(
        f"  infra failures: {rep.get('infra_failures_before', '?')} before replay, "
        f"{rep.get('infra_failures_after', '?')} after; "
        f"{len(scored.infra_unresolved)} question(s) unscorable"
    )
    if scored.score.gold_problems:
        lines.append(
            f"  gold unusable for {len(scored.score.gold_problems)} question(s), scored 0"
        )
    return "\n".join(lines)


def render_verdict(
    base: ScoredRun, cand: ScoredRun, v: Verdict, alpha: float = ALPHA
) -> str:
    lines = [
        "",
        f"paired {base.label} (baseline) vs {cand.label} over {v.paired} questions",
        f"  both pass {v.both}   only baseline {v.only_base}   "
        f"only candidate {v.only_cand}   neither {v.neither}",
        f"  exact McNemar two-sided p = {v.p_value:.4f}  (alpha {alpha})",
    ]
    for tier, (both, a, b, neither) in v.per_tier.items():
        lines.append(
            f"    {tier:<12} both {both}  only baseline {a}  only candidate {b}  neither {neither}"
        )
    discordant = v.only_base + v.only_cand
    if v.noise_rate is not None:
        lines.append(
            f"  noise band ({v.noise_source}): the same engine disagrees with itself "
            f"on {v.noise_rate:.1%} of questions (about {v.noise_rate * v.paired:.1f} of "
            f"{v.paired}); here {discordant} questions differ between the arms"
        )
    lines.append(f"\nVERDICT: {v.verdict}")
    lines += [f"  note: {n}" for n in v.notes]
    lines.append(f"  note: {FUTURE_WORK}")
    return "\n".join(lines)


# -------------------------------------------------------------------------- CLI


def _cmd_split(args: argparse.Namespace) -> int:
    info = split_cases(
        Path(args.cases),
        Path(args.out),
        seed=args.seed,
        dev=args.dev,
        canary=args.canary,
    )
    c = info["counts"]
    print(
        f"split {sum(c.values())} questions (seed {args.seed}): dev {c['dev']}, "
        f"heldout {c['heldout']}, canary {c['canary']} -> {args.out}\n"
        f"split id {info['split_id']}; each slice is frozen ({FROZEN_FILE}). "
        "Develop on dev; do not look at heldout until you decide."
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get(INTERNAL_TOKEN_ENV) or None

    def show(case: Case, i: int, run: Run) -> None:
        mark = "pass" if run.passed else f"FAIL ({run.reason})"
        print(f"  {case.id} #{i + 1}: {mark}  {run.seconds:.0f}s", flush=True)

    run_dir = run_slice(
        Path(args.split), args.slice, label=args.label, url=args.url,
        out_dir=Path(args.out_dir), workers=args.workers, repeats=args.repeats,
        token=token, git_sha=args.git_sha, notes=args.note or [],
        max_seconds=args.max_seconds, allow_empty_index=args.allow_empty_index, on_run=show,
    )  # fmt: skip
    print(f"\nrun directory: {run_dir}\nscore it: bench score --run {run_dir} ...")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    cand = load_run(Path(args.run))
    base = load_run(Path(args.baseline)) if args.baseline else None
    noise = load_run(Path(args.noise)) if args.noise else None
    scorer, close = make_scorer(
        bool(cand.manifest.get("lsb")), args.dsn_template, args.mode, args.timeout_s
    )
    try:
        scored = score_run(cand, scorer)
        print(render_run(scored))
        if base is None:
            print(f"\nnote: {SLICE_NOTES[str(cand.manifest['slice'])]}")
            return 0
        scored_base = score_run(base, scorer)
        print(render_run(scored_base))
        scored_noise = score_run(noise, scorer) if noise else None
    finally:
        close()
    verdict = compare_scored(scored_base, scored, scored_noise)
    print(render_verdict(scored_base, scored, verdict))
    return 1 if verdict.verdict == REGRESSED else 0


def add_parser(sub: Any) -> None:
    bench = sub.add_parser(
        "bench",
        help="split once, run a slice, score against a baseline (see docs/evaluation.md)",
    )
    steps = bench.add_subparsers(dest="bench_command", required=True)

    sp = steps.add_parser(
        "split", help="deterministic stratified dev/heldout/canary split"
    )
    sp.add_argument("--cases", required=True, help="directory of case files")
    sp.add_argument("--out", required=True, help="new directory for the split")
    sp.add_argument("--seed", type=int, required=True)
    sp.add_argument("--dev", type=float, default=0.3)
    sp.add_argument("--canary", type=float, default=0.1)
    sp.set_defaults(func=_guarded(_cmd_split))

    rp = steps.add_parser("run", help="run every database of a slice in one process")
    rp.add_argument("--split", required=True)
    rp.add_argument("--slice", choices=SLICES, required=True)
    rp.add_argument("--label", required=True, help="the engine under test")
    rp.add_argument("--url", default="http://localhost:3001")
    rp.add_argument("--token", help=f"backend internal token (or {INTERNAL_TOKEN_ENV})")
    rp.add_argument(
        "--workers",
        default="auto",
        help="'auto' or N; capped by the backend's cap and 6",
    )
    rp.add_argument("--repeats", type=int, default=1)
    rp.add_argument("--out-dir", default="bench-runs")
    rp.add_argument("--git-sha", help="backend git sha, when no endpoint reports it")
    rp.add_argument(
        "--note", action="append", help="a knob or remark for the manifest (repeatable)"
    )
    rp.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    rp.add_argument("--allow-empty-index", action="store_true")
    rp.set_defaults(func=_guarded(_cmd_run))

    sc = steps.add_parser(
        "score", help="score a run; with --baseline, paired test and verdict"
    )
    sc.add_argument("--run", required=True)
    sc.add_argument("--baseline")
    sc.add_argument(
        "--noise", help="a second run of the baseline engine, to measure noise"
    )
    sc.add_argument("--dsn-template", help="postgresql://.../{db} (LiveSQLBench cases)")
    sc.add_argument("--mode", choices=MODES, default=OFFICIAL)
    sc.add_argument("--timeout-s", type=float, default=60.0)
    sc.set_defaults(func=_guarded(_cmd_score))


def _guarded(
    fn: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except (BenchError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    return wrapper


__all__ = ["add_parser", "compare_scored", "run_slice", "split_cases"]
