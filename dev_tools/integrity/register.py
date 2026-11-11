# SPDX-License-Identifier: Apache-2.0

"""Parse and validate ``docs/verification-status.md``.

The register is only useful if a row cannot say nothing: each one needs a
level from the fixed vocabulary, evidence a reviewer can follow, and a date (or
``never``). A path written as evidence must exist, so a renamed test cannot
leave a row pointing at nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

LEVELS = ("REAL-E2E", "REAL-ENGINE", "FAKES", "UNVERIFIED")
HEADER = ("Capability", "Claim", "Verification level", "Evidence", "Last verified")
REGISTER_PATH = Path("docs") / "verification-status.md"

_CODE_SPAN = re.compile(r"`([^`]+)`")
_PR = re.compile(r"\bPRs?\s*#\d+")


@dataclass(frozen=True)
class Row:
    capability: str
    claim: str
    level: str
    evidence: str
    last_verified: str
    line: int


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_register(text: str) -> list[Row]:
    """Return the rows of the table whose header is ``HEADER``."""
    rows: list[Row] = []
    in_table = False
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if tuple(cells) == HEADER:
            in_table = True
            continue
        if not in_table or set(line.replace("|", "").strip()) <= {"-", ":", " "}:
            continue  # another table, or the separator row
        cells += [""] * (len(HEADER) - len(cells))
        rows.append(Row(*cells[: len(HEADER)], line=number))
    return rows


def evidence_paths(evidence: str) -> list[str]:
    """Backticked values written as repository paths (they contain a slash)."""
    return [
        s.split("::")[0]
        for s in _CODE_SPAN.findall(evidence)
        if "/" in s and " " not in s and not s.startswith("/")
    ]


def _is_date(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_rows(rows: list[Row], repo_root: Path) -> list[str]:
    """Return one message per problem; empty means the register is sound."""
    problems: list[str] = []
    if not rows:
        return ["no register table found"]
    seen: set[str] = set()
    for row in rows:
        where = f"line {row.line} ({row.capability or '?'})"
        if not row.capability or not row.claim:
            problems.append(f"{where}: capability and claim must not be empty")
        if row.capability in seen:
            problems.append(f"{where}: duplicate capability")
        seen.add(row.capability)
        level = row.level.strip("`")
        if level not in LEVELS:
            problems.append(f"{where}: level {row.level!r} is not one of {LEVELS}")
        if not row.evidence.strip():
            problems.append(f"{where}: evidence is empty")
        elif level in ("REAL-E2E", "REAL-ENGINE", "FAKES") and not (
            _CODE_SPAN.search(row.evidence)
            or _PR.search(row.evidence)
            or "eval run" in row.evidence
        ):
            # A claim above UNVERIFIED must point at something to look at.
            problems.append(
                f"{where}: evidence must cite a path, a PR or an eval run for {level}"
            )
        for path in evidence_paths(row.evidence):
            if not (repo_root / path).exists():
                problems.append(f"{where}: evidence path does not exist: {path}")
        if row.last_verified != "never" and not _is_date(row.last_verified):
            problems.append(
                f"{where}: last verified must be YYYY-MM-DD or 'never', "
                f"got {row.last_verified!r}"
            )
    return problems
