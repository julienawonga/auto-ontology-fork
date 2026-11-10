# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from dev_tools.integrity.register import (
    REGISTER_PATH,
    evidence_paths,
    parse_register,
    validate_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HEADER = (
    "| Capability | Claim | Verification level | Evidence | Last verified |\n"
    "|---|---|---|---|---|\n"
)


def _problems(tmp_path: Path, *rows: str) -> list[str]:
    (tmp_path / "real.py").write_text("")
    return validate_rows(parse_register(HEADER + "\n".join(rows) + "\n"), tmp_path)


def test_a_sound_register_has_no_problems(tmp_path: Path) -> None:
    assert not _problems(
        tmp_path,
        "| A | claim | REAL-E2E | `real.py`, PR #1 | 2026-11-10 |",
        "| B | claim | UNVERIFIED | in progress | never |",
    )


def test_the_levels_table_is_not_mistaken_for_the_register() -> None:
    text = "| Level | Meaning |\n|---|---|\n| `FAKES` | x |\n"
    assert parse_register(text) == []


def test_an_unknown_level_is_rejected(tmp_path: Path) -> None:
    out = _problems(tmp_path, "| A | c | PROBABLY-FINE | PR #1 | 2026-11-10 |")
    assert any("not one of" in p for p in out)


def test_empty_evidence_is_rejected(tmp_path: Path) -> None:
    out = _problems(tmp_path, "| A | c | UNVERIFIED |  | never |")
    assert any("evidence is empty" in p for p in out)


def test_a_verified_claim_needs_something_to_look_at(tmp_path: Path) -> None:
    out = _problems(tmp_path, "| A | c | REAL-E2E | trust me | 2026-11-10 |")
    assert any("must cite" in p for p in out)


def test_an_evidence_path_that_does_not_exist_is_rejected(tmp_path: Path) -> None:
    out = _problems(tmp_path, "| A | c | FAKES | `gone/test_x.py` | 2026-11-10 |")
    assert any("does not exist: gone/test_x.py" in p for p in out)


def test_a_bad_date_is_rejected(tmp_path: Path) -> None:
    out = _problems(tmp_path, "| A | c | UNVERIFIED | none | yesterday |")
    assert any("YYYY-MM-DD" in p for p in out)


def test_duplicate_capabilities_are_rejected(tmp_path: Path) -> None:
    out = _problems(
        tmp_path,
        "| A | c | UNVERIFIED | none | never |",
        "| A | c | UNVERIFIED | none | never |",
    )
    assert any("duplicate" in p for p in out)


def test_a_missing_table_is_reported(tmp_path: Path) -> None:
    assert validate_rows(parse_register("no table here"), tmp_path) == [
        "no register table found"
    ]


def test_only_slash_containing_code_spans_are_paths() -> None:
    assert evidence_paths("`a/b.py::test_x`, `TRINO_TEST_URL`, PR #3") == ["a/b.py"]


def test_the_committed_register_is_valid() -> None:
    text = (REPO_ROOT / REGISTER_PATH).read_text(encoding="utf-8")
    rows = parse_register(text)
    assert len(rows) >= 20
    assert validate_rows(rows, REPO_ROOT) == []
