# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_tools.evals.cases import DEFAULT_CASES_DIR, load_cases, parse_case

CASE_FILES = sorted(DEFAULT_CASES_DIR.glob("*.json"))


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_case_files_are_valid(path: Path) -> None:
    cases = load_cases(path)
    assert cases, "a case file must hold at least one case"
    for case in cases:
        assert case.question.strip()
        assert all(
            g.sql.strip().lower().startswith(("select", "with")) for g in case.gold
        ), f"{case.id}: gold SQL must be a read-only query"


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_every_gold_answer_has_a_snapshot(path: Path) -> None:
    for case in load_cases(path):
        case.accepted()  # raises when a snapshot is missing


def test_duplicate_gold_labels_are_rejected() -> None:
    raw = {
        "id": "x",
        "question": "q",
        "gold": [{"label": "a", "sql": "select 1"}, {"label": "a", "sql": "select 2"}],
    }
    with pytest.raises(ValueError, match="duplicate gold labels"):
        parse_case(raw)


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    case = {"id": "x", "question": "q", "gold": [{"label": "a", "sql": "select 1"}]}
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"cases": [case, case]}))
    with pytest.raises(ValueError, match="duplicate case ids"):
        load_cases(path)
