# SPDX-License-Identifier: Apache-2.0

"""LiveSQLBench tooling, on synthetic data only: no real question, gold or rows."""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dev_tools.evals import __main__ as cli
from dev_tools.evals.cases import Case, GoldAnswer, load_cases
from dev_tools.evals.lsb import (
    GoldResult,
    PostgresExecutor,
    build_cases,
    format_knowledge,
    mcnemar_exact,
    normalize_sql,
    normalize_value,
    paired_counts,
    render_scores,
    repair_trailing_comma,
    rows_match,
    score_report,
    score_sql,
    strip_comments,
    strip_distinct,
    strip_round,
    write_case_files,
)
from dev_tools.evals.report import CaseResult, Report, Run

# ---------------------------------------------------------------- SQL rewriting


def test_strip_comments_keeps_comment_markers_inside_strings() -> None:
    sql = "SELECT '-- not a comment', 'a/*b*/' -- trailing\nFROM t /* block\n*/ WHERE x = 1"
    out = strip_comments(sql)
    assert "'-- not a comment'" in out and "'a/*b*/'" in out
    assert "trailing" not in out and "block" not in out
    assert "FROM t" in out and "WHERE x = 1" in out


def test_strip_round_simple_and_case_insensitive() -> None:
    assert strip_round("SELECT ROUND(price, 2) FROM t") == "SELECT (price) FROM t"
    assert strip_round("SELECT round (price , 2)") == "SELECT (price )"


def test_strip_round_with_nested_parentheses_and_inner_commas() -> None:
    sql = "SELECT ROUND(SUM(COALESCE(a, 0) * (b + c)), 2) AS s FROM t"
    assert strip_round(sql) == "SELECT (SUM(COALESCE(a, 0) * (b + c))) AS s FROM t"


def test_strip_round_nested_round_and_casts() -> None:
    assert strip_round("SELECT ROUND(ROUND(a, 3) + b, 1)") == "SELECT ((a) + b)"
    assert (
        strip_round("SELECT ROUND(a::numeric, 2)::text") == "SELECT (a::numeric)::text"
    )


def test_strip_round_keeps_precedence() -> None:
    assert strip_round("SELECT 2 * ROUND(a + b, 2)") == "SELECT 2 * (a + b)"


def test_strip_round_leaves_one_argument_round_and_strings_alone() -> None:
    assert strip_round("SELECT ROUND(a)") == "SELECT ROUND(a)"
    assert strip_round("SELECT 'ROUND(a, 2)'") == "SELECT 'ROUND(a, 2)'"
    # an array argument holds a comma that is not an argument separator
    assert strip_round("SELECT ROUND((ARRAY[1,2])[1], 2)") == "SELECT ((ARRAY[1,2])[1])"


def test_strip_round_with_unbalanced_parentheses_is_left_alone() -> None:
    assert strip_round("SELECT ROUND(a, 2") == "SELECT ROUND(a, 2"


def test_strip_distinct_removes_the_select_modifier_only() -> None:
    assert strip_distinct("SELECT DISTINCT a FROM t").replace("  ", " ") == (
        "SELECT a FROM t"
    )
    sub = strip_distinct("SELECT a FROM (SELECT DISTINCT b AS a FROM t) s")
    assert "DISTINCT" not in sub


def test_strip_distinct_keeps_distinct_on() -> None:
    sql = "SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b"
    assert strip_distinct(sql) == sql


def test_strip_distinct_keeps_aggregates_and_is_distinct_from_by_default() -> None:
    assert "COUNT(DISTINCT x)" in strip_distinct("SELECT COUNT(DISTINCT x) FROM t")
    assert "IS DISTINCT FROM" in strip_distinct("SELECT 1 WHERE a IS DISTINCT FROM b")
    assert "'DISTINCT'" in strip_distinct("SELECT 'DISTINCT' FROM t")


def test_strip_distinct_inside_aggregates_is_opt_in() -> None:
    out = strip_distinct("SELECT COUNT(DISTINCT x) FROM t", aggregates=True)
    assert "DISTINCT" not in out and "COUNT(" in out


def test_normalize_sql_applies_everything_and_drops_the_semicolon() -> None:
    sql = "SELECT DISTINCT ROUND(AVG(x), 2) -- note\nFROM t;"
    out = normalize_sql(sql)
    assert "DISTINCT" not in out and "ROUND" not in out and "note" not in out
    assert not out.endswith(";")


def test_repair_trailing_comma_only_before_from() -> None:
    assert repair_trailing_comma("SELECT a, b, FROM t") == "SELECT a, b FROM t"
    assert repair_trailing_comma("SELECT a, b FROM t") == "SELECT a, b FROM t"
    assert repair_trailing_comma("SELECT ', FROM' FROM t") == "SELECT ', FROM' FROM t"


# ------------------------------------------------------------ value normalising


def test_two_decimal_rounding_is_half_up_and_absorbs_float_noise() -> None:
    # The motivating pair: the same number computed two ways.
    assert normalize_value(-2.2765) == normalize_value(-2.27650000000004)
    assert normalize_value(-2.2765) == Decimal("-2.28")
    assert normalize_value(2.675) == Decimal("2.68")  # half up, not binary float
    assert normalize_value(Decimal("0.005")) == Decimal("0.01")
    assert normalize_value(0.004999) == Decimal("0.00")
    assert normalize_value(0.0049999999) != normalize_value(0.005)


def test_numbers_of_every_type_meet() -> None:
    assert (
        normalize_value(3) == normalize_value(3.0) == normalize_value(Decimal("3.001"))
    )
    assert hash(normalize_value(3)) == hash(normalize_value(Decimal("3.00")))
    assert normalize_value(-0.001) == normalize_value(0)


def test_huge_and_non_finite_numbers_do_not_raise() -> None:
    assert normalize_value(Decimal("1" + "0" * 40 + ".555")) is not None
    assert normalize_value(float("nan")) == "nan"
    assert normalize_value(float("inf")) == "inf"
    assert normalize_value(Decimal("NaN")) == "NaN"


def test_dates_become_plain_dates() -> None:
    assert normalize_value(dt.date(2024, 3, 5)) == "2024-03-05"
    assert normalize_value(dt.datetime(2024, 3, 5, 13, 45, 1)) == "2024-03-05"


def test_nested_values_are_json_dumped_and_bool_stays_bool() -> None:
    assert normalize_value({"b": 1, "a": [1, 2]}) == '{"a": [1, 2], "b": 1}'
    assert normalize_value([1, "x"]) == '[1, "x"]'
    assert normalize_value(True) is True and normalize_value(None) is None
    assert normalize_value("Text ") == "Text "  # strings are not folded


# ------------------------------------------------------------------- comparing


def test_rows_compare_as_sets_unless_ordered() -> None:
    a, b = [[1, "x"], [2, "y"]], [[2, "y"], [1, "x"]]
    assert rows_match(a, b, ordered=False)
    assert not rows_match(a, b, ordered=True)
    assert rows_match(a, a, ordered=True)


def test_set_comparison_collapses_duplicates() -> None:
    assert rows_match([[1], [1], [2]], [[1], [2]], ordered=False)
    assert not rows_match([[1], [1], [2]], [[1], [2]], ordered=True)


def test_column_order_and_values_matter() -> None:
    assert not rows_match([[1, 2]], [[2, 1]], ordered=False)
    assert not rows_match([[1.234]], [[1.24]], ordered=False)
    assert rows_match([[1.234]], [[1.2349]], ordered=False)  # both 1.23


def _case(
    cid: str = "q1", *, ordered: bool = False, tier: str = "Simple", db: str = "d1"
) -> Case:
    return Case(
        id=cid,
        question="q",
        gold=(GoldAnswer("gold", "SELECT DISTINCT ROUND(x, 2) FROM t -- c"),),
        ordered=ordered,
        tags=(f"tier:{tier}",),
        target_db=db,
    )


class FakeDb:
    """Maps normalised SQL to rows; anything else raises, like a bad query."""

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def __call__(self, database: str, sql: str) -> list[list[Any]]:
        self.calls.append((database, sql))
        outcome = self.results[sql]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


GOLD_SQL = normalize_sql("SELECT DISTINCT ROUND(x, 2) FROM t -- c")


def test_score_sql_reasons() -> None:
    case = _case()
    gold = GoldResult([[1.0], [2.0]])
    db = FakeDb(
        {
            "match": [[2.001], [1.0]],
            "values": [[3.0]],
            "empty": [],
            "boom": RuntimeError("syntax"),
        }
    )
    run = lambda sql: score_sql(case, sql, gold, db).reason  # noqa: E731
    assert run("match") == "match"
    assert run("values") == "values"
    assert run("empty") == "pred_empty"
    assert run("boom") == "pred_error"
    assert run(None) == "no_sql" and run("  ") == "no_sql"


def test_score_sql_order_and_gold_problems() -> None:
    gold = GoldResult([[1], [2]])
    db = FakeDb({"s": [[2], [1]]})
    assert score_sql(_case(ordered=True), "s", gold, db).reason == "order"
    assert score_sql(_case(ordered=False), "s", gold, db).passed
    assert score_sql(_case(), "s", GoldResult(None, "err"), db).reason == "gold_error"
    assert score_sql(_case(), "s", GoldResult([]), db).reason == "gold_empty"


def test_predicted_sql_is_normalised_and_run_on_the_cases_database() -> None:
    case = _case(db="archeology")
    db = FakeDb({"SELECT  (x) FROM t": [[1]]})
    out = score_sql(case, "SELECT DISTINCT ROUND(x, 2) FROM t;", GoldResult([[1]]), db)
    assert out.passed
    assert db.calls[0][0] == "archeology"
    assert "DISTINCT" not in db.calls[0][1] and "ROUND" not in db.calls[0][1]


def _report(label: str, sqls: dict[str, list[str | None]]) -> Report:
    report = Report(meta={"label": label})
    for cid, runs in sqls.items():
        report.cases[cid] = CaseResult(
            "q", [Run(False, "x", sql=s, seconds=10.0) for s in runs]
        )
    return report


def test_score_report_tiers_runs_and_gold_executed_once() -> None:
    cases = [
        _case("a", tier="Simple"),
        _case("b", tier="Simple"),
        _case("c", tier="Challenging"),
    ]
    db = FakeDb({GOLD_SQL: [[1]], "good": [[1]], "bad": [[9]]})
    report = _report("eng", {"a": ["good"], "b": ["bad"], "c": ["good", "bad", "good"]})
    score = score_report(report, cases, db)
    tiers = score.tier_scores()
    assert tiers["Simple"] == (2, 0.5)
    assert tiers["Challenging"][0] == 1 and math.isclose(tiers["Challenging"][1], 2 / 3)
    assert tiers["Overall"][0] == 3
    assert list(tiers) == ["Simple", "Challenging", "Overall"]
    assert [c for _, c in db.calls].count(GOLD_SQL) == 3  # once per case, not per run
    assert score.question_passed("c") and not score.question_passed("b")


def test_score_report_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="not in the cases"):
        score_report(_report("e", {"zzz": ["x"]}), [_case("a")], FakeDb({}))


def test_unusable_gold_scores_zero_and_is_reported() -> None:
    db = FakeDb({GOLD_SQL: RuntimeError("syntax error")})
    score = score_report(_report("e", {"a": ["x"]}), [_case("a")], db)
    assert score.tier_scores()["Overall"] == (1, 0.0)
    assert "a" in score.gold_problems
    assert "gold unusable" in render_scores([score])


# ------------------------------------------------------------------- statistics


def test_mcnemar_exact_values() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(5, 0) == pytest.approx(2 / 32)
    assert mcnemar_exact(0, 5) == mcnemar_exact(5, 0)
    # 15 against 8 discordant questions
    expected = 2 * sum(math.comb(23, k) for k in range(9)) / 2**23
    assert mcnemar_exact(15, 8) == pytest.approx(expected)
    assert 0.2 < mcnemar_exact(15, 8) < 0.22
    assert mcnemar_exact(10, 0) == pytest.approx(2 / 1024)


def test_paired_counts_use_the_shared_questions() -> None:
    cases = [_case(c) for c in "abcd"]
    db = FakeDb({GOLD_SQL: [[1]], "good": [[1]], "bad": [[9]]})
    one = score_report(
        _report("one", {"a": ["good"], "b": ["good"], "c": ["bad"], "d": ["good"]}),
        cases,
        db,
    )
    two = score_report(
        _report("two", {"a": ["good"], "b": ["bad"], "c": ["bad"]}), cases, db
    )
    p = paired_counts(one, two)
    assert (p.both, p.only_a, p.only_b, p.neither) == (1, 1, 0, 1)
    text = render_scores([one, two])
    assert "McNemar" in text and "do not hold the same questions" in text


# --------------------------------------------------------------------- building

PUBLIC = [
    {
        "instance_id": "alpha_1",
        "selected_database": "alpha",
        "query": "How many widgets?  Verbatim, with  spaces.",
        "category": "Query",
        "difficulty_tier": "Simple",
    },
    {
        "instance_id": "alpha_2",
        "selected_database": "alpha",
        "query": "List widgets by size.",
        "category": "Query",
        "difficulty_tier": "Challenging",
    },
    {
        "instance_id": "beta_1",
        "selected_database": "beta",
        "query": "Add a column.",
        "category": "Management",
        "difficulty_tier": "Simple",
    },
    {
        "instance_id": "beta_2",
        "selected_database": "beta",
        "query": "Count gadgets.",
        "category": "Query",
        "difficulty_tier": "Moderate",
    },
]
GT = [
    {
        "instance_id": "alpha_1",
        "sol_sql": ["SELECT 1 FROM w"],
        "external_knowledge": [3, 7],
        "conditions": {"order": False},
    },
    {
        "instance_id": "alpha_2",
        "sol_sql": ["SELECT a, b, FROM w ORDER BY a"],
        "external_knowledge": [],
        "conditions": json.dumps({"order": True}),
    },
    {"instance_id": "beta_1", "sol_sql": ["ALTER TABLE g ADD c int"]},
    {
        "instance_id": "beta_2",
        "sol_sql": ["SELECT 2", "SELECT 3"],
        "external_knowledge": [1],
    },
]
KB_ALPHA = [
    {
        "id": 3,
        "knowledge": "Widget Index",
        "description": "How widgy",
        "definition": "a / b",
    },
    {"id": 7, "knowledge": "Size", "description": "Width", "definition": "w * 2"},
    {"id": 9, "knowledge": "Unused", "description": "u", "definition": "u"},
]


def _kb_dir(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    (kb / "alpha").mkdir(parents=True)
    (kb / "alpha" / "alpha_kb.jsonl").write_text(
        "\n".join(json.dumps(e) for e in KB_ALPHA) + "\n", encoding="utf-8"
    )
    return kb


def test_build_without_oracle_has_no_evidence_and_needs_no_kb() -> None:
    result = build_cases(PUBLIC, GT, None)
    assert sorted(result.databases) == ["alpha"]  # beta_2 skipped, beta_1 not a SELECT
    cases = {c["id"]: c for c in result.databases["alpha"]}
    assert set(cases) == {"alpha_1", "alpha_2"}
    assert all("evidence" not in c for c in cases.values())
    assert cases["alpha_1"]["question"] == PUBLIC[0]["query"]
    assert cases["alpha_1"]["gold"] == [{"label": "gold", "sql": "SELECT 1 FROM w"}]
    assert cases["alpha_1"]["ordered"] is False and cases["alpha_2"]["ordered"] is True
    assert cases["alpha_1"]["database"] == "alpha"
    assert "tier:Simple" in cases["alpha_1"]["tags"]
    assert result.skipped == ["beta_2 (2 sol_sql)"]


def test_build_repairs_the_comma_and_says_so() -> None:
    result = build_cases(PUBLIC, GT, None)
    case = {c["id"]: c for c in result.databases["alpha"]}["alpha_2"]
    assert ", FROM" not in case["gold"][0]["sql"].upper()
    assert result.repaired == ["alpha_2"]
    assert "repaired" in case["description"]


def test_build_with_oracle_formats_the_knowledge_entries(tmp_path: Path) -> None:
    result = build_cases(PUBLIC, GT, _kb_dir(tmp_path), oracle=True)
    cases = {c["id"]: c for c in result.databases["alpha"]}
    assert cases["alpha_1"]["evidence"] == (
        "Widget Index: How widgy Definition: a / b\nSize: Width Definition: w * 2"
    )
    assert "evidence" not in cases["alpha_2"]  # no knowledge, nothing to send
    assert format_knowledge(KB_ALPHA[1]) == "Size: Width Definition: w * 2"


def test_build_fails_loudly_on_missing_truth_or_knowledge(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no ground truth"):
        build_cases(PUBLIC, GT[1:], None)
    gt = [dict(GT[0], external_knowledge=[404]), *GT[1:]]
    with pytest.raises(ValueError, match="not in the alpha KB"):
        build_cases(PUBLIC, gt, _kb_dir(tmp_path), oracle=True)
    with pytest.raises(ValueError, match="knowledge-base directory"):
        build_cases(PUBLIC, GT, None, oracle=True)


def test_written_files_load_as_cases_without_rows_yet(tmp_path: Path) -> None:
    result = build_cases(PUBLIC, GT, _kb_dir(tmp_path), oracle=True)
    [path] = write_case_files(result, tmp_path / "out", oracle=True)
    assert path.name == "alpha.json"
    cases = load_cases(path)
    assert [c.target_db for c in cases] == ["alpha", "alpha"]
    assert cases[0].evidence.startswith("Widget Index")
    with pytest.raises(ValueError, match="no snapshot"):
        cases[0].accepted()


def test_cli_build_writes_one_file_per_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    public, gt = tmp_path / "pub.jsonl", tmp_path / "gt.jsonl"
    public.write_text("\n".join(json.dumps(r) for r in PUBLIC[:2]) + "\n")
    gt.write_text("\n".join(json.dumps(r) for r in GT[:2]) + "\n")
    out = tmp_path / "out"
    code = cli.main(
        [
            "lsb-build", "--public", str(public), "--gt", str(gt),
            "--kb-dir", str(_kb_dir(tmp_path)), "--out", str(out), "--oracle",
        ]
    )  # fmt: skip
    assert code == 0
    assert [p.name for p in out.iterdir()] == ["alpha.json"]
    assert "2 SELECT cases" in capsys.readouterr().out


def test_cli_build_refuses_an_output_inside_the_repository(tmp_path: Path) -> None:
    code = cli.main(
        [
            "lsb-build", "--public", "x", "--gt", "y", "--kb-dir", "z",
            "--out", str(cli.REPO_ROOT / "dev_tools" / "evals" / "cases" / "lsb"),
        ]
    )  # fmt: skip
    assert code == 2


def test_executor_needs_a_database_placeholder() -> None:
    with pytest.raises(ValueError, match=r"\{db\}"):
        PostgresExecutor("postgresql://u:p@h/fixed")
