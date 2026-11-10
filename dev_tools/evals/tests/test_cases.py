# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from dev_tools.evals.cases import (
    DEFAULT_CASES_DIR,
    jsonable,
    load_cases,
    parse_case,
    run_gold,
    snapshot_file,
)

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


def test_case_ids_are_unique_across_files() -> None:
    # A merged report is keyed by id, so two files sharing one would collide.
    ids = [c.id for path in CASE_FILES for c in load_cases(path)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("path", CASE_FILES, ids=lambda p: p.name)
def test_case_ids_carry_the_dataset_prefix(path: Path) -> None:
    dataset = json.loads(path.read_text(encoding="utf-8"))["dataset"]
    assert all(c.id.startswith(f"{dataset}.") for c in load_cases(path))


@pytest.fixture
def sqlite_dsn(tmp_path: Path) -> str:
    db = tmp_path / "tiny.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE t (id INTEGER, name TEXT, price NUMERIC, note TEXT, born DATETIME);"
        "INSERT INTO t VALUES (1, 'a', 1.5, NULL, '2024-01-02 00:00:00'),"
        " (2, 'b', 2.5, 'x', '2024-01-03 00:00:00');"
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db.as_posix()}"


def test_run_gold_reads_sqlite_rows(sqlite_dsn: str) -> None:
    rows = run_gold(sqlite_dsn, "SELECT id, name, price, note FROM t ORDER BY id")
    assert rows == [[1, "a", 1.5, None], [2, "b", 2.5, "x"]]


def test_run_gold_sqlite_is_read_only(sqlite_dsn: str) -> None:
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        run_gold(sqlite_dsn, "DELETE FROM t")
    assert run_gold(sqlite_dsn, "SELECT COUNT(*) FROM t") == [[2]]


def test_run_gold_sqlite_missing_file_is_an_error(tmp_path: Path) -> None:
    # mode=ro must not create an empty database for a mistyped path.
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        run_gold(f"sqlite:///{missing.as_posix()}", "SELECT 1")
    assert not missing.exists()


def test_jsonable_handles_database_values() -> None:
    assert jsonable(Decimal("1.50")) == 1.5
    assert jsonable(dt.date(2024, 1, 2)) == "2024-01-02"
    assert jsonable(None) is None
    assert jsonable(b"ab") == "b'ab'"


def test_snapshot_fills_then_checks_sqlite(sqlite_dsn: str, tmp_path: Path) -> None:
    case = {
        "id": "x.total",
        "question": "q",
        "gold": [{"label": "a", "sql": "SELECT SUM(price) FROM t"}],
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"cases": [case]}))
    # nothing stored yet, so a check reports the answer as stale
    assert snapshot_file(path, sqlite_dsn, check=True) == ["x.total/a"]
    assert snapshot_file(path, sqlite_dsn) == []
    assert json.loads(path.read_text())["cases"][0]["gold"][0]["expected"] == [[4.0]]
    assert snapshot_file(path, sqlite_dsn, check=True) == []

    db = sqlite3.connect(sqlite_dsn.removeprefix("sqlite:///"))
    db.execute("UPDATE t SET price = 10 WHERE id = 1")
    db.commit()
    db.close()
    assert snapshot_file(path, sqlite_dsn, check=True) == ["x.total/a"]
