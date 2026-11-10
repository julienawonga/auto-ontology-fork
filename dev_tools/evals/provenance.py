# SPDX-License-Identifier: Apache-2.0

"""Decide whether two reports measured the same thing.

A score only means something next to another score taken under the same
conditions. Two conditions are recorded in every report's meta and checked here:
the state of the ontology index (an empty index measures the raw schema) and the
cases file (a different file is a different test). A report that predates the
fields is "unknown": comparing it is allowed, with a warning, because refusing
would strand every existing report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dev_tools.evals.preflight import index_state
from dev_tools.evals.report import Report


def report_index_state(meta: dict[str, Any]) -> str | None:
    """``populated``, ``empty`` or ``incomplete``; ``None`` when not recorded."""
    index = meta.get("ontology_index")
    return index_state(index) if isinstance(index, list) else None


def report_case_hashes(meta: dict[str, Any]) -> frozenset[str] | None:
    """The cases-file hashes behind a report (several after a merge)."""
    value = meta.get("cases_sha256")
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and value:
        return frozenset(str(v) for v in value)
    return None


@dataclass
class Comparability:
    mismatches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_comparable(base: Report, candidate: Report) -> Comparability:
    """List what differs between two reports, and what cannot be told."""
    out = Comparability()

    base_state, cand_state = (
        report_index_state(base.meta),
        report_index_state(candidate.meta),
    )
    if base_state is None or cand_state is None:
        who = " and ".join(
            n for n, s in (("base", base_state), ("candidate", cand_state)) if s is None
        )
        out.warnings.append(
            f"ontology index state unknown for {who}: no state is recorded (the report "
            "predates the pre-flight, or it was overridden), so it may have "
            "measured the raw schema"
        )
    elif base_state != cand_state:
        out.mismatches.append(
            f"ontology index differs: base {base_state}, candidate {cand_state}"
        )

    base_cases, cand_cases = (
        report_case_hashes(base.meta),
        report_case_hashes(candidate.meta),
    )
    if base_cases is None or cand_cases is None:
        out.warnings.append(
            "cases file unknown for at least one report: it predates the hash, so "
            "it may have been scored against different cases"
        )
    elif base_cases != cand_cases:
        out.mismatches.append(
            "cases files differ: base "
            f"{', '.join(sorted(h[:12] for h in base_cases))}, candidate "
            f"{', '.join(sorted(h[:12] for h in cand_cases))}. Re-score the baseline "
            "with the candidate's cases (`rescore`) before comparing."
        )
    return out
