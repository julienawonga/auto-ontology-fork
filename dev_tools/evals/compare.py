# SPDX-License-Identifier: Apache-2.0

"""Decide whether an answer's rows are the same answer as the expected rows.

The SQL text is never compared: two queries that return the same rows are the
same answer, and the same query written differently is not worth failing over.
Rows are compared as *values*, which is deliberately tolerant of things that do
not change the answer:

* column names and column order (``revenue`` vs ``total``),
* row order, unless the case says the order is part of the question,
* case and surrounding whitespace in strings (``RUTH MARTINEZ`` vs ``Ruth Martinez``),
* numeric noise beyond four significant figures (``115.272`` vs ``115.3``),
* a midnight timestamp vs the bare date.

and strict about what does:

* a different number of rows or columns (an extra column is a failure unless the
  case opts in with ``allow_extra_columns``),
* different values,
* an empty result where rows were expected.

Four significant figures is the tolerance Databricks documents for its Genie
benchmarks; it is a rounding, so a value sitting exactly on a rounding boundary
can still mismatch its neighbour. That is rare enough to accept and is why every
case is run several times.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Sequence

# Beyond this many rows a comparison stops being a statement about the answer
# and becomes a memory problem; the chat itself returns the full frame today.
MAX_COMPARED_ROWS = 5000

_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00:00(\.0+)?(Z|[+-]00:?00)?$")

Cell = tuple[Any, ...]
Row = tuple[Cell, ...]

Reason = Literal[
    "match",
    "no_result",
    "empty_result",
    "unexpected_rows",
    "row_count",
    "column_count",
    "values",
    "order",
]


def normalize_cell(value: Any) -> Cell:
    """Reduce one cell to a hashable, orderable canonical form.

    The leading tag keeps the types apart so that sorting a row never has to
    compare a number with a string.
    """
    if value is None:
        return ("n",)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float, Decimal)):
        return ("num", _round_sig(float(value)))
    if isinstance(value, str):
        text = " ".join(value.split())
        if _NUMERIC_RE.match(text):
            return ("num", _round_sig(float(text)))
        midnight = _MIDNIGHT_RE.match(text)
        if midnight:
            return ("s", midnight.group(1))
        return ("s", text.casefold())
    return ("s", str(value).casefold())


def _round_sig(value: float) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        return value
    return float(f"{value:.4g}")


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[Row]:
    """Normalize rows; cells within a row are sorted so column order is ignored."""
    return [tuple(sorted(normalize_cell(cell) for cell in row)) for row in rows]


@dataclass(frozen=True)
class Verdict:
    """Outcome of comparing one answer with one expected result."""

    passed: bool
    reason: Reason
    detail: str = ""
    missing: list[list[Any]] = field(default_factory=list)
    unexpected: list[list[Any]] = field(default_factory=list)


def _show(row: Row) -> list[Any]:
    return [cell[1] if len(cell) > 1 else None for cell in row]


def compare_rows(
    actual: Sequence[Sequence[Any]] | None,
    expected: Sequence[Sequence[Any]],
    *,
    ordered: bool = False,
    allow_extra_columns: bool = False,
    allow_missing_columns: bool = False,
) -> Verdict:
    """Compare *actual* rows with *expected* rows. ``None`` means no result at all."""
    if actual is None:
        return Verdict(False, "no_result", "the agent returned no executed result")
    if len(actual) > MAX_COMPARED_ROWS or len(expected) > MAX_COMPARED_ROWS:
        actual = actual[:MAX_COMPARED_ROWS]
        expected = expected[:MAX_COMPARED_ROWS]

    if not expected:
        if not actual:
            return Verdict(True, "match")
        return Verdict(False, "unexpected_rows", f"expected no rows, got {len(actual)}")
    if not actual:
        return Verdict(False, "empty_result", f"expected {len(expected)} row(s), got 0")

    if len(actual) != len(expected):
        return Verdict(
            False, "row_count", f"expected {len(expected)} row(s), got {len(actual)}"
        )

    actual_width = len(actual[0])
    expected_width = len(expected[0])
    if actual_width != expected_width and not (
        (allow_extra_columns and actual_width > expected_width)
        or (allow_missing_columns and actual_width < expected_width)
    ):
        return Verdict(
            False,
            "column_count",
            f"expected {expected_width} column(s), got {actual_width}",
        )

    if actual_width == expected_width:
        return _compare_same_width(actual, expected, ordered)
    if actual_width > expected_width:
        return _compare_with_extra_columns(actual, expected)
    # Narrower answer (e.g. the category without its total): every actual row
    # must be contained in a distinct expected row.
    return _compare_with_extra_columns(expected, actual, swapped=True)


def _compare_same_width(
    actual: Sequence[Sequence[Any]],
    expected: Sequence[Sequence[Any]],
    ordered: bool,
) -> Verdict:
    got = Counter(normalize_rows(actual))
    want = Counter(normalize_rows(expected))
    if got != want:
        return Verdict(
            False,
            "values",
            "the rows differ from the expected rows",
            missing=[_show(r) for r in (want - got).elements()][:5],
            unexpected=[_show(r) for r in (got - want).elements()][:5],
        )
    # Column order is never significant, so order is judged on the *sequence of
    # rows* as seen through the sorted-cell view.
    if ordered and normalize_rows(actual) != normalize_rows(expected):
        return Verdict(False, "order", "the rows match but are in a different order")
    return Verdict(True, "match")


def _compare_with_extra_columns(
    actual: Sequence[Sequence[Any]],
    expected: Sequence[Sequence[Any]],
    *,
    swapped: bool = False,
) -> Verdict:
    """Every expected row must be contained in a distinct actual row.

    With ``swapped`` the roles are reversed: the arguments are (wider=expected,
    narrower=actual) and the narrower rows are the ones that must be found.
    """
    remaining = [Counter(row) for row in normalize_rows(actual)]
    missing: list[list[Any]] = []
    for want_row in normalize_rows(expected):
        want = Counter(want_row)
        for i, have in enumerate(remaining):
            if not (want - have):
                del remaining[i]
                break
        else:
            missing.append(_show(want_row))
    if missing:
        return Verdict(
            False,
            "values",
            "expected rows not found in the result",
            missing=missing[:5],
        )
    return Verdict(True, "match")


@dataclass(frozen=True)
class AnyVerdict:
    """Outcome against several accepted answers; ``matched`` names the one that hit."""

    passed: bool
    matched: str | None
    verdicts: dict[str, Verdict]

    @property
    def reason(self) -> Reason:
        if self.passed:
            return "match"
        # Report the reason of the closest miss, so "why did it fail" is about
        # the primary answer rather than an arbitrary alternative.
        first = next(iter(self.verdicts.values()))
        return first.reason


def compare_any(
    actual: Sequence[Sequence[Any]] | None,
    accepted: dict[str, Sequence[Sequence[Any]]],
    *,
    ordered: bool = False,
    allow_extra_columns: bool = False,
    allow_missing_columns: bool = False,
) -> AnyVerdict:
    """Pass when *actual* equals any accepted answer.

    A question can legitimately have several answers (``"store 2"`` meaning the
    inventory's store or the customer's), so a case carries all of them.
    """
    verdicts = {
        label: compare_rows(
            actual,
            rows,
            ordered=ordered,
            allow_extra_columns=allow_extra_columns,
            allow_missing_columns=allow_missing_columns,
        )
        for label, rows in accepted.items()
    }
    matched = next((label for label, v in verdicts.items() if v.passed), None)
    return AnyVerdict(matched is not None, matched, verdicts)
