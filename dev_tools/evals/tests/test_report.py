# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from dev_tools.evals.report import CaseResult, Report, Run, compare_reports


def _report(label: str, rates: dict[str, tuple[int, int]]) -> Report:
    """rates: case id -> (passes, runs)."""
    report = Report(meta={"label": label})
    for cid, (passes, total) in rates.items():
        runs = [Run(True, "match") for _ in range(passes)] + [
            Run(False, "values") for _ in range(total - passes)
        ]
        report.cases[cid] = CaseResult(f"question {cid}", runs)
    return report


def test_score_is_mean_pass_rate_per_question() -> None:
    report = _report("x", {"a": (3, 3), "b": (0, 3), "c": (1, 2)})
    assert round(report.score, 4) == round((1 + 0 + 0.5) / 3, 4)


def test_improvement_is_accepted() -> None:
    base = _report("base", {"a": (1, 3), "b": (3, 3)})
    cand = _report("cand", {"a": (3, 3), "b": (3, 3)})
    result = compare_reports(base, cand)
    assert result.accepted and len(result.improvements) == 1 and not result.regressions


def test_a_regression_rejects_even_when_the_total_improves() -> None:
    # The aggregate rises (a: +2/3, b: -1) but b regressed; that must not hide.
    base = _report("base", {"a": (1, 3), "b": (3, 3), "c": (0, 3)})
    cand = _report("cand", {"a": (3, 3), "b": (0, 3), "c": (3, 3)})
    result = compare_reports(base, cand)
    assert result.delta > 0
    assert [c.case_id for c in result.regressions] == ["b"]
    assert not result.accepted


def test_tolerance_lets_one_flaky_run_through_but_not_a_real_drop() -> None:
    base = _report("base", {"a": (3, 3), "b": (3, 3)})
    cand = _report("cand", {"a": (2, 3), "b": (0, 3)})
    result = compare_reports(base, cand, tolerance=0.34)
    assert [c.case_id for c in result.regressions] == ["b"]
    assert result.unchanged == 1


def test_margin_is_required_overall_gain() -> None:
    base = _report("base", {"a": (2, 3)})
    cand = _report("cand", {"a": (2, 3)})
    assert compare_reports(base, cand, margin=0.0).accepted
    assert not compare_reports(base, cand, margin=0.05).accepted


def test_only_shared_questions_are_compared() -> None:
    base = _report("base", {"a": (3, 3), "old": (0, 3)})
    cand = _report("cand", {"a": (3, 3), "new": (3, 3)})
    result = compare_reports(base, cand)
    assert result.base_score == result.candidate_score == 1.0
    assert result.only_in_base == ["old"] and result.only_in_candidate == ["new"]


def test_report_round_trips_through_json(tmp_path: Path) -> None:
    report = _report("x", {"a": (2, 3)})
    path = tmp_path / "r.json"
    report.save(path)
    loaded = Report.load(path)
    assert loaded.cases["a"].pass_rate == report.cases["a"].pass_rate
    assert loaded.meta["score"] == round(report.score, 4)


def test_merge_replaces_cases_from_later_reports() -> None:
    from dev_tools.evals.report import merge_reports

    def report(label: str, **rates: bool) -> Report:
        return Report(
            meta={"label": label},
            cases={
                cid: CaseResult("q", [Run(ok, "match" if ok else "values")])
                for cid, ok in rates.items()
            },
        )

    merged = merge_reports(
        "m", [report("a", x=False, y=True), report("b", x=True, z=True)]
    )
    assert set(merged.cases) == {"x", "y", "z"}
    assert merged.cases["x"].pass_rate == 1.0
    assert merged.meta["merged_from"] == ["a", "b"]
