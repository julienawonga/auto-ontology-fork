# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dev_tools.evals import __main__ as cli
from dev_tools.evals.cases import Case
from dev_tools.evals.preflight import PreflightError
from dev_tools.evals.provenance import check_comparable
from dev_tools.evals.report import CaseResult, Report, Run, merge_reports

POPULATED = [{"database_name": "d", "expected": 8, "indexed": 8, "missing": 0}]
EMPTY = [{"database_name": "d", "expected": 8, "indexed": 0, "missing": 8}]


def _report(label: str, **meta: Any) -> Report:
    report = Report(meta={"label": label, **meta})
    report.cases["a"] = CaseResult("q", [Run(True, "match")])
    return report


def test_matching_provenance_is_comparable() -> None:
    a = _report("a", ontology_index=POPULATED, cases_sha256="h1")
    b = _report("b", ontology_index=POPULATED, cases_sha256="h1")
    result = check_comparable(a, b)
    assert not result.mismatches and not result.warnings


def test_populated_against_empty_is_a_mismatch() -> None:
    a = _report("a", ontology_index=EMPTY, cases_sha256="h1")
    b = _report("b", ontology_index=POPULATED, cases_sha256="h1")
    assert "base empty, candidate populated" in check_comparable(a, b).mismatches[0]


def test_different_cases_files_are_a_mismatch() -> None:
    a = _report("a", ontology_index=POPULATED, cases_sha256="h1")
    b = _report("b", ontology_index=POPULATED, cases_sha256="h2")
    assert "cases files differ" in check_comparable(a, b).mismatches[0]


def test_reports_without_the_new_meta_are_unknown_not_refused() -> None:
    result = check_comparable(_report("old"), _report("new", ontology_index=EMPTY))
    assert not result.mismatches
    assert len(result.warnings) == 2  # index state and cases file


def test_merge_keeps_both_case_hashes_and_all_index_entries() -> None:
    merged = merge_reports(
        "m",
        [
            _report("a", ontology_index=POPULATED, cases_sha256="h1"),
            _report("b", ontology_index=EMPTY, cases_sha256="h2"),
        ],
    )
    assert merged.meta["cases_sha256"] == ["h1", "h2"]
    assert len(merged.meta["ontology_index"]) == 2
    # one populated and one empty part is not a populated report
    other = _report("c", ontology_index=POPULATED, cases_sha256="h1")
    assert check_comparable(merged, other).mismatches


def test_merge_with_one_undocumented_part_is_unknown() -> None:
    merged = merge_reports(
        "m", [_report("a", ontology_index=POPULATED, cases_sha256="h1"), _report("b")]
    )
    assert "cases_sha256" not in merged.meta and "ontology_index" not in merged.meta


def test_merge_of_identical_hashes_stays_a_string() -> None:
    merged = merge_reports(
        "m", [_report("a", cases_sha256="h1"), _report("b", cases_sha256="h1")]
    )
    assert merged.meta["cases_sha256"] == "h1"


def _save(tmp_path: Path, name: str, **meta: Any) -> str:
    path = tmp_path / f"{name}.json"
    _report(name, **meta).save(path)
    return str(path)


def test_compare_refuses_mismatched_reports_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _save(tmp_path, "base", ontology_index=EMPTY, cases_sha256="h")
    cand = _save(tmp_path, "cand", ontology_index=POPULATED, cases_sha256="h")
    assert cli.main(["compare", base, cand]) == 2
    err = capsys.readouterr().err
    assert "not comparable" in err and "base empty, candidate populated" in err


def test_compare_force_prints_the_mismatch_and_compares(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _save(tmp_path, "base", ontology_index=EMPTY, cases_sha256="h")
    cand = _save(tmp_path, "cand", ontology_index=POPULATED, cases_sha256="h")
    assert cli.main(["compare", base, cand, "--force"]) == 0
    out = capsys.readouterr().out
    assert "FORCED comparison" in out and "base empty, candidate populated" in out
    assert "verdict:" in out


def test_compare_of_old_reports_warns_but_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["compare", _save(tmp_path, "a"), _save(tmp_path, "b")]) == 0
    assert "WARNING" in capsys.readouterr().err


def _cases_file(tmp_path: Path) -> str:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "a",
                        "question": "q",
                        "gold": [{"label": "p", "sql": "S", "expected": [[1]]}],
                    }
                ]
            }
        )
    )
    return str(path)


def test_run_does_not_start_when_the_preflight_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise PreflightError("index is empty")

    def never(*args: Any, **kwargs: Any) -> Report:
        raise AssertionError("a question was sent")

    monkeypatch.setattr(cli, "preflight", refuse)
    monkeypatch.setattr(cli, "run_cases", never)
    out = tmp_path / "r.json"
    code = cli.main(["run", "--cases", _cases_file(tmp_path), "--out", str(out)])
    assert code == 2 and "index is empty" in capsys.readouterr().err
    assert not out.exists()


def test_run_records_the_provenance_and_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_preflight(url: str, target: str | None, **kw: Any) -> dict[str, Any]:
        seen["allow"] = kw["allow_empty_index"]
        return {"backend_url": url, "ontology_index": EMPTY, "allow_empty_index": True}

    def fake_run(cases: list[Case], url: str, **kw: Any) -> Report:
        return _report("x", **kw["provenance"])

    monkeypatch.setattr(cli, "preflight", fake_preflight)
    monkeypatch.setattr(cli, "run_cases", fake_run)
    out = tmp_path / "r.json"
    cases = _cases_file(tmp_path)
    assert (
        cli.main(["run", "--cases", cases, "--allow-empty-index", "--out", str(out)])
        == 0
    )
    meta = Report.load(out).meta
    assert seen["allow"] is True and meta["allow_empty_index"] is True
    assert meta["ontology_index"] == EMPTY
    assert meta["cases_sha256"] and meta["cases_file"] == "cases.json"


def test_rescore_stamps_the_cases_it_scored_against(tmp_path: Path) -> None:
    report = Report(meta={"label": "x", "cases_sha256": "old"})
    report.cases["a"] = CaseResult("q", [Run(True, "match", rows=1, actual=[[1]])])
    src = tmp_path / "in.json"
    report.save(src)
    out = tmp_path / "out.json"
    cases = _cases_file(tmp_path)
    assert cli.main(["rescore", str(src), "--cases", cases, "--out", str(out)]) == 0
    assert Report.load(out).meta["cases_sha256"] != "old"
