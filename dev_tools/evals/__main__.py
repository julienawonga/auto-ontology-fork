# SPDX-License-Identifier: Apache-2.0

"""Command line for the evaluation harness.

# 1. fill (or verify) the expected rows from the source database
python -m dev_tools.evals snapshot --dsn postgresql://postgres:postgres@localhost:5432/pagila
python -m dev_tools.evals snapshot --check --dsn ...
# a SQLite source works too (opened read-only)
python -m dev_tools.evals snapshot --cases dev_tools/evals/cases/chinook.json --dsn sqlite:///path/to/chinook.sqlite

# 2. score a running backend, three runs per question
python -m dev_tools.evals run --label baseline --out reports/baseline.json

# 3. patch a baseline with re-run questions (later reports win)
python -m dev_tools.evals merge base.json rerun.json --out merged.json

# 4. the same runs under another convention, without re-running
python -m dev_tools.evals rescore run.json --strict --out run-strict.json

# 5. compare a candidate with a baseline; non-zero exit when it is rejected
python -m dev_tools.evals compare reports/baseline.json reports/candidate.json --margin 0.02

# LiveSQLBench-Base-Lite (inputs and outputs stay outside the repository)
python -m dev_tools.evals lsb-build --public data.jsonl --gt gt.jsonl --kb-dir kb --out lsb --oracle
python -m dev_tools.evals run --cases lsb/*.json --workers 4 --label main --out main.json
python -m dev_tools.evals replay main.json --cases lsb/*.json
# arm C: the KB stored as ontology Terms of one database instead of sent as evidence
python -m dev_tools.evals lsb-kb --kb-dir kb --db alien --dry-run   # then --apply / --remove
python -m dev_tools.evals lsb-score --reports main.replayed.json cand.replayed.json \
    --cases lsb/*.json --dsn-template postgresql://USER:PASSWORD@HOST:5432/{db}
#   (official metric by default; --mode clean for our stricter reading of it)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from auto_ontology.server.internal_auth import (
    INTERNAL_TOKEN_ENV,
    INTERNAL_TOKEN_HEADER,
)
from dev_tools.evals.cases import (
    DEFAULT_CASES_DIR,
    Case,
    load_many,
    snapshot_file,
)
from dev_tools.evals.lsb import (
    CLEAN,
    MODES,
    OFFICIAL,
    OFFICIAL_FETCH_CAP,
    PostgresExecutor,
    build_cases,
    load_jsonl,
    merge_scores,
    render_scores,
    score_report,
    write_case_files,
)
from dev_tools.evals.preflight import PreflightError, preflight
from dev_tools.evals.provenance import check_comparable
from dev_tools.evals.report import Report, Run, compare_reports, merge_reports
from dev_tools.evals.replay import DEFAULT_MAX_SECONDS, replay_report, select_replay
from dev_tools.evals.runner import MAX_WORKERS, clamp_workers, rescore, run_cases
from dev_tools.integrity.freeze import file_cases_sha256

DEFAULT_CASES = DEFAULT_CASES_DIR / "pagila.json"
DEFAULT_URL = "http://localhost:3001"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _case_paths(values: list[str]) -> list[Path]:
    """Case files; a directory stands for the ``*.json`` files in it."""
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        paths += sorted(path.glob("*.json")) if path.is_dir() else [path]
    return paths


def _hash_cases(paths: list[Path]) -> str | list[str]:
    hashes = sorted(file_cases_sha256(p) for p in paths)
    return hashes[0] if len(hashes) == 1 else hashes


def _preflight_all(
    url: str,
    cases: list[Case],
    target_db: str | None,
    *,
    allow_empty_index: bool,
    token: str | None = None,
) -> dict[str, Any]:
    """Pre-flight every database the cases target (one call for a single one)."""
    if target_db:
        targets: list[str | None] = [target_db]
    elif cases and all(c.target_db for c in cases):
        targets = sorted({c.target_db for c in cases})
    else:
        targets = [None]
    parts = [
        preflight(url, t, allow_empty_index=allow_empty_index, token=token)
        for t in targets
    ]
    if len(parts) == 1:
        return parts[0]
    merged: dict[str, Any] = {"backend_url": url}
    if all(isinstance(p.get("ontology_index"), list) for p in parts):
        merged["ontology_index"] = [e for p in parts for e in p["ontology_index"]]
    if any(p.get("allow_empty_index") for p in parts):
        merged["allow_empty_index"] = True
        merged["ontology_index_problems"] = [
            q for p in parts for q in p.get("ontology_index_problems", [])
        ]
    return merged


def _cmd_snapshot(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get("EVAL_SOURCE_DSN")
    if not dsn:
        print("error: pass --dsn or set EVAL_SOURCE_DSN", file=sys.stderr)
        return 2
    drifted = snapshot_file(Path(args.cases), dsn, check=args.check)
    if args.check:
        if drifted:
            print("snapshot is stale for:\n  " + "\n  ".join(drifted))
            return 1
        print("snapshot is up to date")
    else:
        print(f"snapshot written to {args.cases}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # A backend gated by INTERNAL_API_TOKEN is called directly here, not
    # through the web app, so the token must be presented explicitly.
    token = args.token or os.environ.get(INTERNAL_TOKEN_ENV) or None
    paths = [Path(p) for p in args.cases]
    try:
        cases = load_many(paths)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        cases = [c for c in cases if any(t in c.id for t in args.only)]
    for case in cases:
        case.accepted()  # fail fast if a snapshot is missing

    try:
        provenance = _preflight_all(
            args.url,
            cases,
            args.target_db,
            allow_empty_index=args.allow_empty_index,
            token=token,
        )
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for problem in provenance.get("ontology_index_problems", []):
        print(f"WARNING (--allow-empty-index): {problem}", file=sys.stderr)
    provenance["cases_sha256"] = _hash_cases(paths)
    provenance["cases_file"] = (
        paths[0].name if len(paths) == 1 else [p.name for p in paths]
    )
    workers = clamp_workers(args.workers)
    if workers != args.workers:
        print(
            f"WARNING: --workers {args.workers} -> {workers} "
            f"(more than {MAX_WORKERS} exhausts the backend's connections)",
            file=sys.stderr,
        )

    def show(case: Case, i: int, run: Run) -> None:
        mark = "pass" if run.passed else f"FAIL ({run.reason})"
        print(f"  {case.id} #{i + 1}: {mark}  {run.seconds:.0f}s", flush=True)

    report = run_cases(
        cases,
        args.url,
        label=args.label,
        repeats=args.repeats,
        target_db=args.target_db,
        token=token,
        on_run=show,
        provenance=provenance,
        workers=workers,
    )
    report.save(Path(args.out))
    print(
        f"\nscore {report.score:.1%} over {len(report.cases)} questions -> {args.out}"
    )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    base, candidate = Report.load(Path(args.base)), Report.load(Path(args.candidate))
    comparable = check_comparable(base, candidate)
    for warning in comparable.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if comparable.mismatches:
        listing = "\n  ".join(comparable.mismatches)
        if not args.force:
            print(
                "error: the reports measured different things, so their scores "
                f"are not comparable:\n  {listing}\n"
                "Fix the mismatch, or pass --force to compare anyway.",
                file=sys.stderr,
            )
            return 2
        print(
            f"FORCED comparison, the reports measured different things:\n  {listing}\n"
        )
    comparison = compare_reports(
        base, candidate, margin=args.margin, tolerance=args.tolerance
    )
    print(comparison.render())
    return 0 if comparison.accepted else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    merged = merge_reports(args.label, [Report.load(Path(p)) for p in args.reports])
    merged.save(Path(args.out))
    print(f"score {merged.score:.1%} over {len(merged.cases)} questions -> {args.out}")
    return 0


def _cmd_rescore(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.cases]
    report = rescore(
        Report.load(Path(args.report)), load_many(paths), strict=args.strict
    )
    # The runs are now scored against these files, whatever they were run with.
    report.meta["cases_sha256"] = _hash_cases(paths)
    report.save(Path(args.out))
    print(f"score {report.score:.1%} over {len(report.cases)} questions -> {args.out}")
    return 0


def _inside_repo(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == REPO_ROOT or REPO_ROOT in resolved.parents


def _cmd_lsb_build(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if _inside_repo(out):
        print(
            "error: --out is inside this repository. The gold SQL and anything "
            "built from it must stay outside it (no-redistribution request).",
            file=sys.stderr,
        )
        return 2
    if args.dsn_template and "{db}" not in args.dsn_template:
        print("error: --dsn-template must contain {db}", file=sys.stderr)
        return 2
    try:
        result = build_cases(
            load_jsonl(Path(args.public)),
            load_jsonl(Path(args.gt)),
            Path(args.kb_dir) if args.kb_dir else None,
            oracle=args.oracle,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    paths = write_case_files(result, out, oracle=args.oracle)
    mode = "with knowledge as evidence (--oracle)" if args.oracle else "no evidence"
    print(
        f"{result.total} SELECT cases in {len(paths)} database files, {mode} -> {out}"
    )
    for path in paths:
        print(f"  {path.name}")
    if result.repaired:
        print(f"gold repaired (comma before FROM): {', '.join(result.repaired)}")
    if result.skipped:
        print(f"SKIPPED, not a single sol_sql: {', '.join(result.skipped)}")
    if args.dsn_template:
        failed = 0
        for path in paths:
            db = path.stem
            try:
                snapshot_file(path, args.dsn_template.format(db=db))
            except Exception as exc:  # a gold query that fails is reported, not fatal
                failed += 1
                print(f"snapshot failed for {db}: {exc}", file=sys.stderr)
        if failed:
            return 1
        print("expected rows filled from the databases")
    else:
        print("no expected rows yet: pass --dsn-template, or run `snapshot` per file")
    return 0


def _cmd_lsb_kb(args: argparse.Namespace) -> int:
    import httpx

    from dev_tools.evals import lsb_kb

    kb_dir = Path(args.kb_dir)
    if _inside_repo(kb_dir):
        print(
            "error: --kb-dir is inside this repository; the log is written there",
            file=sys.stderr,
        )
        return 2
    try:
        kb = lsb_kb.check_db(args.db, kb_dir)
        token = args.token or os.environ.get(INTERNAL_TOKEN_ENV) or None
        headers = {INTERNAL_TOKEN_HEADER: token} if token else {}
        with httpx.Client(headers=headers, timeout=60) as client:
            api = lsb_kb.Api(client, args.url)
            if args.apply:
                result = lsb_kb.apply(api, args.db, kb)
            elif args.remove:
                result = lsb_kb.remove(api, args.db)
            else:
                result = lsb_kb.dry_run(api, args.db, kb)
    except (lsb_kb.KbToolError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result["write_requests"] = list(api.writes)
    print(json.dumps(result, indent=2, default=str))
    log = lsb_kb.write_log(kb_dir, args.db, result)
    print(f"log: {log}")
    return 1 if result.get("failures") else 0


def _cmd_lsb_score(args: argparse.Namespace) -> int:
    if "{db}" not in args.dsn_template:
        print("error: --dsn-template must contain {db}", file=sys.stderr)
        return 2
    paths = _case_paths(args.cases)
    try:
        cases = load_many(paths)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    reports = [Report.load(Path(p)) for p in args.reports]
    # A report measured on a different cases file must not be scored silently
    # against this one: its runs answer different questions.
    hashes = {file_cases_sha256(p): p.name for p in paths}
    for name, report in zip(args.reports, reports):
        label = str(report.meta.get("label") or Path(name).name)
        recorded = report.meta.get("cases_sha256")
        if recorded is None:
            continue
        wanted = recorded if isinstance(recorded, list) else [recorded]
        unknown = [h for h in wanted if h not in hashes]
        if unknown:
            message = (
                f"{label}: the report was measured on a cases file that is not "
                f"among --cases ({len(unknown)} of its hashes match none of them)"
            )
            if not args.allow_cases_mismatch:
                print(
                    f"error: {message}; pass --allow-cases-mismatch to score anyway",
                    file=sys.stderr,
                )
                return 2
            print(f"WARNING (--allow-cases-mismatch): {message}", file=sys.stderr)
    fetch_cap = OFFICIAL_FETCH_CAP if args.mode == OFFICIAL else None
    executor = PostgresExecutor(args.dsn_template, args.timeout_s, fetch_cap=fetch_cap)
    gold_cache: dict[str, Any] = {}
    try:
        # each report resolves its own target_db fallback before the scores of
        # reports that share a label are combined into one
        scores = merge_scores(
            [
                score_report(r, cases, executor, mode=args.mode, gold_cache=gold_cache)
                for r in reports
            ]
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        executor.close()
    print(f"mode: {args.mode}")
    print(render_scores(scores))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get(INTERNAL_TOKEN_ENV) or None
    cases = load_many(_case_paths(args.cases))
    by_id = {c.id: c for c in cases}
    for path in map(Path, args.reports):
        report = Report.load(path)
        selected = select_replay(report, args.max_seconds)
        label = report.meta.get("label", path.stem)
        if not selected:
            print(f"{label}: nothing to replay")
            continue
        url = args.url or report.meta.get("backend_url")
        if not url:
            print(
                f"error: {path.name} records no backend_url; pass --url",
                file=sys.stderr,
            )
            return 2
        replayed = [by_id[cid] for cid, _ in selected if cid in by_id]
        try:
            _preflight_all(
                url,
                replayed,
                args.target_db,
                allow_empty_index=args.allow_empty_index,
                token=token,
            )
        except PreflightError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        def show(case: Case, i: int, run: Run) -> None:
            mark = "pass" if run.passed else f"FAIL ({run.reason})"
            print(f"  {case.id} #{i + 1}: {mark}  {run.seconds:.0f}s", flush=True)

        print(f"{label}: replaying {len(selected)} run(s) against {url}")
        try:
            merged, _ = replay_report(
                report,
                cases,
                url,
                max_seconds=args.max_seconds,
                target_db=args.target_db,
                workers=clamp_workers(args.workers),
                token=token,
                on_run=show,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out_dir = Path(args.out_dir) if args.out_dir else path.parent
        out = out_dir / f"{path.stem}.replayed.json"
        merged.save(out)
        print(f"  score {report.score:.1%} -> {merged.score:.1%} -> {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.evals", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser(
        "snapshot", help="fill or verify expected rows from the database"
    )
    snap.add_argument("--cases", default=str(DEFAULT_CASES))
    snap.add_argument(
        "--dsn", help="Postgres DSN or sqlite:///path (or EVAL_SOURCE_DSN)"
    )
    snap.add_argument(
        "--check", action="store_true", help="verify only; exit 1 if stale"
    )
    snap.set_defaults(func=_cmd_snapshot)

    run = sub.add_parser("run", help="score a running backend")
    run.add_argument(
        "--cases",
        nargs="+",
        default=[str(DEFAULT_CASES)],
        help="one or more case files (ids must be unique across them)",
    )
    run.add_argument(
        "--workers",
        type=int,
        default=1,
        help=f"concurrent chat requests (capped at {MAX_WORKERS}: more exhausts "
        "the backend's database connections)",
    )
    run.add_argument("--url", default=DEFAULT_URL, help="backend base URL")
    run.add_argument(
        "--token",
        help=f"backend internal token, if it requires one (or {INTERNAL_TOKEN_ENV})",
    )
    run.add_argument("--target-db", help="catalog database name or UUID")
    run.add_argument("--repeats", type=int, default=3, help="runs per question")
    run.add_argument("--label", default="run")
    run.add_argument("--out", default="eval-report.json")
    run.add_argument("--only", nargs="*", help="run only case ids containing these")
    run.add_argument(
        "--allow-empty-index",
        action="store_true",
        help="run although the semantic index is empty, partial or unreadable "
        "(the report records that you did)",
    )
    run.set_defaults(func=_cmd_run)

    cmp_ = sub.add_parser("compare", help="paired comparison of two reports")
    cmp_.add_argument("base")
    cmp_.add_argument("candidate")
    cmp_.add_argument("--margin", type=float, default=0.0, help="required overall gain")
    cmp_.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="per-question pass-rate drop tolerated before it counts as a regression",
    )
    cmp_.add_argument(
        "--force",
        action="store_true",
        help="compare although the reports measured different things "
        "(index state or cases file); the mismatch is printed",
    )
    cmp_.set_defaults(func=_cmd_compare)

    resc = sub.add_parser(
        "rescore", help="re-score a saved report from its stored rows"
    )
    resc.add_argument("report")
    resc.add_argument("--cases", nargs="+", default=[str(DEFAULT_CASES)])
    resc.add_argument(
        "--strict", action="store_true", help="ignore per-case column tolerances"
    )
    resc.add_argument("--out", required=True)
    resc.set_defaults(func=_cmd_rescore)

    merge = sub.add_parser(
        "merge", help="combine partial reports; later reports replace earlier cases"
    )
    merge.add_argument("reports", nargs="+", help="reports, oldest first")
    merge.add_argument("--label", default="merged")
    merge.add_argument("--out", required=True)
    merge.set_defaults(func=_cmd_merge)

    build = sub.add_parser(
        "lsb-build",
        help="build per-database case files from LiveSQLBench-Base-Lite SELECT tasks",
    )
    build.add_argument("--public", required=True, help="livesqlbench_data.jsonl")
    build.add_argument("--gt", required=True, help="ground-truth jsonl (kept private)")
    build.add_argument(
        "--kb-dir", help="dir holding <db>/<db>_kb.jsonl; required with --oracle"
    )
    build.add_argument("--out", required=True, help="output dir, outside this repo")
    build.add_argument(
        "--oracle",
        action="store_true",
        help="send each task's knowledge entries as `evidence`",
    )
    build.add_argument(
        "--dsn-template",
        help="postgresql://.../{db}: also fill the expected rows (snapshot)",
    )
    build.set_defaults(func=_cmd_lsb_build)

    kbp = sub.add_parser(
        "lsb-kb",
        help="load a LiveSQLBench KB into one database's ontology as Terms (arm C)",
    )
    kbp.add_argument("--kb-dir", required=True, help="dir with <db>/<db>_kb.jsonl")
    kbp.add_argument("--db", required=True, help="one database name")
    kbp.add_argument("--url", default="http://127.0.0.1:3001")
    kbp.add_argument(
        "--token",
        help=f"backend internal token, if it requires one (or {INTERNAL_TOKEN_ENV})",
    )
    mode = kbp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read-only preview")
    mode.add_argument("--apply", action="store_true", help="create Terms + reindex")
    mode.add_argument(
        "--remove", action="store_true", help="delete our Terms + reindex"
    )
    kbp.set_defaults(func=_cmd_lsb_kb)

    lsb = sub.add_parser(
        "lsb-score", help="score reports with LiveSQLBench's SELECT metric"
    )
    lsb.add_argument("--reports", nargs="+", required=True)
    lsb.add_argument(
        "--cases", nargs="+", required=True, help="case files (or dirs) from lsb-build"
    )
    lsb.add_argument(
        "--dsn-template", required=True, help="postgresql://.../{db}, one per database"
    )
    lsb.add_argument(
        "--timeout-s", type=float, default=60.0, help="statement timeout per query"
    )
    lsb.add_argument(
        "--mode",
        choices=MODES,
        default=OFFICIAL,
        help=f"'{OFFICIAL}' (default) transcribes the benchmark's own metric, "
        f"quirks included; '{CLEAN}' is our more careful reading of the same "
        "published rule (see docs/evaluation.md for every difference)",
    )
    lsb.add_argument(
        "--allow-cases-mismatch",
        action="store_true",
        help="score although a report's cases_sha256 matches none of the --cases "
        "files (the report was measured on different questions)",
    )
    lsb.set_defaults(func=_cmd_lsb_score)

    rep = sub.add_parser(
        "replay",
        help="re-run runs that errored with no SQL in under N seconds",
    )
    rep.add_argument("reports", nargs="+", help="reports; originals are not modified")
    rep.add_argument("--cases", nargs="+", required=True)
    rep.add_argument("--url", help="backend URL (default: the one the report recorded)")
    rep.add_argument(
        "--token",
        help=f"backend internal token, if it requires one (or {INTERNAL_TOKEN_ENV})",
    )
    rep.add_argument("--target-db")
    rep.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    rep.add_argument("--workers", type=int, default=1)
    rep.add_argument("--out-dir", help="default: next to each report")
    rep.add_argument("--allow-empty-index", action="store_true")
    rep.set_defaults(func=_cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
