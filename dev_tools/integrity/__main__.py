# SPDX-License-Identifier: Apache-2.0

"""Integrity checks: nothing faked, nothing fitted to the evaluation.

python -m dev_tools.integrity                 # run every check (what CI runs)
python -m dev_tools.integrity freeze FILE     # freeze an eval case file

``check`` fails when
  * the shipped code or resources contain an eval question, or an expected
    value or gold literal that is not in ``allowlist.json`` (fitting the test);
  * an eval case file that declares ``frozen_sha256`` no longer matches it;
  * ``docs/verification-status.md`` has a malformed row or cites a missing path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dev_tools.integrity.freeze import check_frozen, freeze
from dev_tools.integrity.register import REGISTER_PATH, parse_register, validate_rows
from dev_tools.integrity.scan import scan_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = Path("dev_tools") / "evals" / "cases"
ALLOWLIST = Path(__file__).parent / "allowlist.json"


def run_checks(root: Path, allowlist: Path | None = ALLOWLIST) -> list[str]:
    """Return one message per problem; empty means every check passed."""
    cases_dir = root / CASES_DIR
    problems = [h.render() for h in scan_tree(root, cases_dir, allowlist)]
    for path in sorted(cases_dir.glob("*.json")):
        message = check_frozen(path)
        if message:
            problems.append(message)
    register = root / REGISTER_PATH
    if register.exists():
        rows = parse_register(register.read_text(encoding="utf-8"))
        problems += [f"{REGISTER_PATH}: {m}" for m in validate_rows(rows, root)]
    else:
        problems.append(f"{REGISTER_PATH}: missing")
    return problems


def _cmd_check(args: argparse.Namespace) -> int:
    problems = run_checks(Path(args.root))
    if problems:
        print("integrity check FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print("integrity check passed")
    return 0


def _cmd_freeze(args: argparse.Namespace) -> int:
    digest = freeze(Path(args.file))
    print(f"froze {args.file} (sha256 {digest})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.integrity", description=__doc__
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    parser.set_defaults(func=_cmd_check)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="run every check (the default)").set_defaults(
        func=_cmd_check
    )
    frz = sub.add_parser("freeze", help="record a case file's hash and date")
    frz.add_argument("file")
    frz.set_defaults(func=_cmd_freeze)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
