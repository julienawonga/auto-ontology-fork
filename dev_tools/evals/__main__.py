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
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dev_tools.evals.cases import DEFAULT_CASES_DIR, Case, load_cases, snapshot_file
from dev_tools.evals.preflight import PreflightError, preflight
from dev_tools.evals.provenance import check_comparable
from dev_tools.evals.report import Report, Run, compare_reports, merge_reports
from dev_tools.evals.runner import rescore, run_cases
from dev_tools.integrity.freeze import file_cases_sha256

DEFAULT_CASES = DEFAULT_CASES_DIR / "pagila.json"
DEFAULT_URL = "http://localhost:3001"


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
    cases = load_cases(Path(args.cases))
    if args.only:
        cases = [c for c in cases if any(t in c.id for t in args.only)]
    for case in cases:
        case.accepted()  # fail fast if a snapshot is missing

    try:
        provenance = preflight(
            args.url, args.target_db, allow_empty_index=args.allow_empty_index
        )
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for problem in provenance.get("ontology_index_problems", []):
        print(f"WARNING (--allow-empty-index): {problem}", file=sys.stderr)
    provenance["cases_sha256"] = file_cases_sha256(Path(args.cases))
    provenance["cases_file"] = Path(args.cases).name

    def show(case: Case, i: int, run: Run) -> None:
        mark = "pass" if run.passed else f"FAIL ({run.reason})"
        print(f"  {case.id} #{i + 1}: {mark}  {run.seconds:.0f}s", flush=True)

    report = run_cases(
        cases,
        args.url,
        label=args.label,
        repeats=args.repeats,
        target_db=args.target_db,
        on_run=show,
        provenance=provenance,
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
    report = rescore(
        Report.load(Path(args.report)),
        load_cases(Path(args.cases)),
        strict=args.strict,
    )
    # The runs are now scored against this file, whatever they were run with.
    report.meta["cases_sha256"] = file_cases_sha256(Path(args.cases))
    report.save(Path(args.out))
    print(f"score {report.score:.1%} over {len(report.cases)} questions -> {args.out}")
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
    run.add_argument("--cases", default=str(DEFAULT_CASES))
    run.add_argument("--url", default=DEFAULT_URL, help="backend base URL")
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
    resc.add_argument("--cases", default=str(DEFAULT_CASES))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
