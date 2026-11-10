# SPDX-License-Identifier: Apache-2.0

"""Find evaluation answers and questions inside the code that ships.

If a question, an expected value or a gold literal appears in a prompt, a
resource or the source, the engine can pass the evaluation by remembering it:
the test has been fitted. This module extracts what the eval cases contain and
looks for it in the production tree.

Matching works on token sequences (casefolded words), so punctuation,
whitespace, line wraps and letter case cannot hide a copy.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# Shipped code and resources. `mcp/` is built into its own image.
SCAN_ROOTS = ("auto_ontology", "frontend", "mcp")
TEXT_SUFFIXES = {
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".html",
}
SKIP_DIRS = {
    "tests",
    "__tests__",
    "__pycache__",
    "node_modules",
    ".next",
    ".venv",
    ".git",
    "e2e",
}
_TEST_FILE = re.compile(
    r"(^test_.*\.py$|_test\.py$|^conftest\.py$|\.(test|spec)\.[jt]sx?$)"
)
MAX_BYTES = 1_000_000

# Words too generic to prove anything when they match on their own.
STOPWORDS = frozenset(
    "true false none null name names title type date year month day time total count "
    "city country store stores film films customer customers rental rentals payment "
    "payments actor actors category categories language english staff address "
    "active inactive yes unknown other default status value values data".split()
)

_SQL_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_TOKEN = re.compile(r"\w+")


def normalise(text: str) -> str:
    """Casefold, drop punctuation, collapse whitespace."""
    return " ".join(_TOKEN.findall(text.casefold()))


def _distinctive(raw: str) -> str | None:
    """The normalised form of *raw* when it is specific enough to look for."""
    norm = normalise(raw)
    if len(raw.strip()) < 4 or not norm:
        return None
    if all(tok.isdigit() for tok in norm.split()):
        return None
    if norm in STOPWORDS:
        return None
    return norm


@dataclass(frozen=True)
class Needle:
    kind: str  # "question" | "expected" | "literal"
    norm: str
    text: str
    source: str  # case file name
    case_id: str


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    needle: Needle

    def render(self) -> str:
        n = self.needle
        return (
            f"{self.path}:{self.line}: {n.kind} {n.text!r} "
            f"from {n.source} ({n.case_id})"
        )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def extract_needles(case_file: Path) -> list[Needle]:
    raw = json.loads(case_file.read_text(encoding="utf-8"))
    out: list[Needle] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, text: str, case_id: str, norm: str | None) -> None:
        key = (kind, norm or "", case_id)
        if norm and key not in seen:
            seen.add(key)
            out.append(Needle(kind, norm, text, case_file.name, case_id))

    for case in raw["cases"]:
        cid = case["id"]
        q = case["question"]
        add("question", q, cid, normalise(q) or None)
        for gold in case.get("gold", []):
            for cell in _strings(gold.get("expected") or []):
                add("expected", cell, cid, _distinctive(cell))
            for lit in _SQL_LITERAL.findall(gold.get("sql", "")):
                lit = lit.replace("''", "'")
                add("literal", lit, cid, _distinctive(lit))
    return out


def load_needles(cases_dir: Path) -> list[Needle]:
    needles: list[Needle] = []
    for path in sorted(cases_dir.glob("*.json")):
        needles += extract_needles(path)
    return needles


@dataclass(frozen=True)
class AllowEntry:
    norm: str
    path: str | None
    reason: str


def load_allowlist(path: Path) -> list[AllowEntry]:
    """Read the reviewed allow-list; an entry without a written reason is refused."""
    if not path.exists():
        return []
    entries: list[AllowEntry] = []
    for i, item in enumerate(json.loads(path.read_text(encoding="utf-8"))["entries"]):
        reason = str(item.get("reason", "")).strip()
        norm = normalise(str(item.get("value", "")))
        if not norm:
            raise ValueError(f"{path.name}: entry {i} has no value")
        if len(reason) < 10:
            raise ValueError(
                f"{path.name}: entry {i} ({item.get('value')!r}) needs a written reason"
            )
        entries.append(AllowEntry(norm, item.get("path"), reason))
    return entries


def _allowed(hit: Hit, allow: list[AllowEntry]) -> bool:
    # A question is the strong signal and cannot be waved through.
    if hit.needle.kind == "question":
        return False
    return any(
        e.norm == hit.needle.norm
        and (e.path is None or fnmatch.fnmatch(hit.path, e.path))
        for e in allow
    )


def production_files(root: Path) -> Iterator[Path]:
    for top in SCAN_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        stack = [base]
        while stack:
            current = stack.pop()
            for entry in sorted(current.iterdir()):
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS:
                        stack.append(entry)
                elif (
                    entry.suffix in TEXT_SUFFIXES
                    and not _TEST_FILE.search(entry.name)
                    and "lock" not in entry.name
                ):
                    yield entry


def find_hits(text: str, needles: list[Needle]) -> list[tuple[int, Needle]]:
    """Return (line, needle) for every needle found in *text*."""
    newlines = [m.start() for m in re.finditer("\n", text)]
    tokens = [
        (m.group().casefold(), bisect.bisect_left(newlines, m.start()) + 1)
        for m in _TOKEN.finditer(text)
    ]
    if not tokens:
        return []
    starts: list[int] = []
    lines: list[int] = []
    pos = 1  # the joined text is padded with a leading space
    for tok, line in tokens:
        starts.append(pos)
        lines.append(line)
        pos += len(tok) + 1
    joined = " " + " ".join(t for t, _ in tokens) + " "
    found: list[tuple[int, Needle]] = []
    for needle in needles:
        idx = joined.find(f" {needle.norm} ")
        while idx != -1:
            found.append((lines[bisect.bisect_right(starts, idx + 1) - 1], needle))
            idx = joined.find(f" {needle.norm} ", idx + 1)
    return found


def scan_tree(
    root: Path, cases_dir: Path, allowlist_path: Path | None = None
) -> list[Hit]:
    """Hits for the case files in *cases_dir* inside the production tree of *root*."""
    needles = load_needles(cases_dir)
    allow = load_allowlist(allowlist_path) if allowlist_path else []
    hits: list[Hit] = []
    for path in production_files(root):
        if path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for line, needle in find_hits(text, needles):
            hit = Hit(rel, line, needle)
            if not _allowed(hit, allow):
                hits.append(hit)
    return hits
