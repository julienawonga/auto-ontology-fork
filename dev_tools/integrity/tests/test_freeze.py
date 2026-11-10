# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from dev_tools.integrity.__main__ import main
from dev_tools.integrity.freeze import (
    cases_sha256,
    check_frozen,
    file_cases_sha256,
    freeze,
)

CASES = [{"id": "a.one", "question": "Q?", "gold": [{"label": "primary", "sql": "S"}]}]


def _file(tmp_path: Path, **extra: object) -> Path:
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"dataset": "x", **extra, "cases": CASES}, indent=2))
    return path


def test_hash_ignores_formatting_and_key_order() -> None:
    reordered = [
        {"gold": [{"sql": "S", "label": "primary"}], "question": "Q?", "id": "a.one"}
    ]
    assert cases_sha256(CASES) == cases_sha256(reordered)


def test_hash_changes_with_any_case_edit() -> None:
    edited = [{**CASES[0], "question": "Q!"}]
    assert cases_sha256(CASES) != cases_sha256(edited)


def test_an_unfrozen_file_is_not_checked(tmp_path: Path) -> None:
    assert check_frozen(_file(tmp_path)) is None


def test_freeze_records_date_and_hash_and_keeps_the_cases(tmp_path: Path) -> None:
    path = _file(tmp_path)
    digest = freeze(path, today=dt.date(2026, 11, 10))
    raw = json.loads(path.read_text())
    assert raw["frozen_at"] == "2026-11-10"
    assert raw["frozen_sha256"] == digest == file_cases_sha256(path)
    assert raw["cases"] == CASES and raw["dataset"] == "x"
    assert check_frozen(path) is None


def test_freezing_twice_is_idempotent_on_the_hash(tmp_path: Path) -> None:
    path = _file(tmp_path)
    assert freeze(path) == freeze(path)


def test_editing_a_frozen_case_is_reported(tmp_path: Path) -> None:
    path = _file(tmp_path)
    freeze(path)
    raw = json.loads(path.read_text())
    raw["cases"][0]["question"] = "Something easier?"
    path.write_text(json.dumps(raw))
    message = check_frozen(path)
    assert message and "cases changed" in message and "freeze" in message


def test_refreezing_is_the_deliberate_way_to_accept_an_edit(tmp_path: Path) -> None:
    path = _file(tmp_path)
    freeze(path)
    raw = json.loads(path.read_text())
    raw["cases"][0]["question"] = "Something else?"
    path.write_text(json.dumps(raw))
    freeze(path)
    assert check_frozen(path) is None


def test_a_half_declared_freeze_is_reported(tmp_path: Path) -> None:
    message = check_frozen(_file(tmp_path, frozen_sha256="abc"))
    assert message and "only one" in message


def test_a_bad_frozen_date_is_reported(tmp_path: Path) -> None:
    message = check_frozen(_file(tmp_path, frozen_at="soon", frozen_sha256="abc"))
    assert message and "ISO date" in message


def test_cli_freeze_writes_the_markers(tmp_path: Path, capsys) -> None:
    path = _file(tmp_path)
    assert main(["freeze", str(path)]) == 0
    assert "frozen_sha256" in json.loads(path.read_text())
    assert "froze" in capsys.readouterr().out


def test_freeze_keeps_lf_line_endings(tmp_path: Path) -> None:
    path = _file(tmp_path)
    freeze(path)
    assert b"\r\n" not in path.read_bytes()
