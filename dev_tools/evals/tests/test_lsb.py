# SPDX-License-Identifier: Apache-2.0

"""LiveSQLBench tooling, on synthetic data only: no real question, gold or rows."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from dev_tools.evals import __main__ as cli
from dev_tools.evals.cases import Case, GoldAnswer, load_cases
from dev_tools.evals.lsb import (
    CLEAN,
    OFFICIAL,
    GoldResult,
    PostgresExecutor,
    assert_single_statement,
    build_cases,
    format_knowledge,
    mcnemar_exact,
    normalize_sql,
    normalize_value,
    official_clean,
    official_strip_comments,
    official_strip_distinct,
    official_strip_round,
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


def test_official_round_strips_both_arities_and_adds_no_parentheses() -> None:
    assert official_strip_round("SELECT ROUND(a)") == "SELECT a"
    assert official_strip_round("SELECT round (price , 2)") == "SELECT price"
    # no parentheses are added, so precedence can change: official behaviour
    assert official_strip_round("SELECT 2 * ROUND(a + b, 2)") == "SELECT 2 * a + b"
    assert official_strip_round("SELECT ROUND(ROUND(a, 3) + b, 1)") == "SELECT a + b"
    assert official_strip_round("SELECT ROUND(a, 2") == "SELECT ROUND(a, 2"


def test_strip_distinct_removes_the_select_modifier_only() -> None:
    assert strip_distinct("SELECT DISTINCT a FROM t").replace("  ", " ") == (
        "SELECT a FROM t"
    )
    sub = strip_distinct("SELECT a FROM (SELECT DISTINCT b AS a FROM t) s")
    assert "DISTINCT" not in sub


def test_strip_distinct_keeps_distinct_on() -> None:
    sql = "SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b"
    assert strip_distinct(sql) == sql


def test_strip_distinct_keeps_aggregates_and_is_distinct_from() -> None:
    # clean mode: only the SELECT modifier goes; official mode breaks both,
    # exactly like the benchmark's own regex (tested below).
    assert "COUNT(DISTINCT x)" in strip_distinct("SELECT COUNT(DISTINCT x) FROM t")
    assert "IS DISTINCT FROM" in strip_distinct("SELECT 1 WHERE a IS DISTINCT FROM b")
    assert "'DISTINCT'" in strip_distinct("SELECT 'DISTINCT' FROM t")


def test_official_distinct_regex_strips_aggregates_and_breaks_is_distinct_from() -> (
    None
):
    # The benchmark's own rule: any DISTINCT not followed (parens excluded) by
    # ON goes, so COUNT(DISTINCT x) loses it and IS DISTINCT FROM stops parsing.
    assert "DISTINCT" not in official_strip_distinct("SELECT COUNT(DISTINCT x) FROM t")
    broken = official_strip_distinct("SELECT 1 WHERE a IS DISTINCT FROM b")
    assert broken == "SELECT 1 WHERE a IS FROM b"  # a syntax error when run


def test_official_distinct_regex_keeps_distinct_on_and_collapses_whitespace() -> None:
    sql = "SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b"
    assert "DISTINCT ON" in official_strip_distinct(sql)
    # whitespace collapses everywhere, string literals included
    assert official_strip_distinct("SELECT 'a \n  b'\nFROM t") == "SELECT 'a b' FROM t"


def test_official_comments_are_regexes_not_quote_aware() -> None:
    # a -- comment without a newline is NOT removed, and -- inside a literal
    # still eats to the end of the line: both quirks of the official code
    assert official_strip_comments("SELECT 1 -- note").endswith("-- note")
    assert official_strip_comments("SELECT '-- x', 2\nFROM t") == "SELECT '\nFROM t"
    assert "/* gone */" not in official_strip_comments("SELECT /* gone */ 1")


def test_normalize_sql_official_is_the_default_and_matches_the_benchmark() -> None:
    sql = "SELECT DISTINCT ROUND(AVG(x), 2) -- note\nFROM t;"
    # comments removed, DISTINCT and both ROUND forms stripped, whitespace
    # collapsed, the trailing semicolon kept (the official code keeps it too)
    assert normalize_sql(sql) == "SELECT AVG(x) FROM t;"


def test_normalize_sql_clean_is_our_stricter_reading() -> None:
    sql = "SELECT DISTINCT ROUND(AVG(x), 2) -- note\nFROM t;"
    out = normalize_sql(sql, mode=CLEAN)
    # DISTINCT and both comments gone, ROUND parenthesised, semicolon dropped;
    # the whitespace they leave behind stays (SQL does not care)
    assert "DISTINCT" not in out and "ROUND" not in out and "note" not in out
    assert "(AVG(x))" in out and not out.endswith(";")


def test_normalize_sql_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        normalize_sql("SELECT 1", mode="strict")  # type: ignore[arg-type]


def test_official_clean_pipeline_matches_the_benchmark_order() -> None:
    # comments, then DISTINCT+whitespace, then ROUND -- in that order
    assert (
        official_clean("SELECT DISTINCT ROUND(x, 2) -- c\nFROM t") == "SELECT x FROM t"
    )


def test_repair_trailing_comma_only_before_from() -> None:
    assert repair_trailing_comma("SELECT a, b, FROM t") == "SELECT a, b FROM t"
    assert repair_trailing_comma("SELECT a, b FROM t") == "SELECT a, b FROM t"
    assert repair_trailing_comma("SELECT ', FROM' FROM t") == "SELECT ', FROM' FROM t"


def test_single_statement_guard_blocks_the_commit_escape() -> None:
    assert_single_statement("SELECT 1")  # fine
    assert_single_statement("SELECT 1;")  # a trailing semicolon is fine
    assert_single_statement("SELECT ';' -- ; not a separator\n")
    assert_single_statement("SELECT $tag$ ; $tag$")
    with pytest.raises(ValueError, match="multiple statements"):
        assert_single_statement("SELECT 1; COMMIT; DROP TABLE x")
    with pytest.raises(ValueError, match="multiple statements"):
        assert_single_statement("SELECT 1; -- done\nSELECT 2")


# ------------------------------------------------------------ value normalising


def test_two_decimal_rounding_is_half_up_and_absorbs_float_noise() -> None:
    # clean mode: every number becomes a Decimal at cents
    v = lambda x: normalize_value(x, mode=CLEAN)  # noqa: E731
    # The motivating pair: the same number computed two ways.
    assert v(-2.2765) == v(-2.27650000000004)
    assert v(-2.2765) == Decimal("-2.28")
    assert v(2.675) == Decimal("2.68")  # half up, not binary float
    assert v(Decimal("0.005")) == Decimal("0.01")
    assert v(0.004999) == Decimal("0.00")
    assert v(0.0049999999) != v(0.005)


def test_official_values_are_rounded_floats_and_ints_stay_ints() -> None:
    v = normalize_value  # official is the default mode
    assert v(-2.2765) == -2.28 and isinstance(v(-2.2765), float)
    assert v(2.675) == 2.68
    assert v(Decimal("0.005")) == 0.01
    assert v(3) == 3 and isinstance(v(3), int)  # ints are not touched
    assert v(3) == v(3.0)  # 3.0 rounds to the float 3.0, still equal


def test_official_rounds_numbers_nested_in_arrays_and_json() -> None:
    # before json.dumps, exactly like the benchmark's own normaliser
    assert normalize_value({"a": [1.005, "x"]}) == '{"a": [1.01, "x"]}'
    assert normalize_value([2.675, [3]]) == "[2.68, [3]]"
    # clean mode dumps the values as they came, without rounding
    assert normalize_value({"a": [1.005]}, mode=CLEAN) == '{"a": [1.005]}'


def test_official_infinities_are_errors_and_nan_never_matches() -> None:
    # the benchmark's quantize raises for infinity and for numbers too large
    # for the default decimal context, so the run scores 0 as an error;
    # NaN flows through as a float that equals nothing, not even itself
    with pytest.raises(InvalidOperation):
        normalize_value(float("inf"))
    with pytest.raises(InvalidOperation):
        normalize_value(Decimal("Infinity"))
    with pytest.raises(InvalidOperation):
        normalize_value(Decimal("1" + "0" * 40 + ".555"))
    nan = normalize_value(float("nan"))
    assert math.isnan(nan) and nan != nan
    # clean mode turns all of them into text instead
    assert normalize_value(float("inf"), mode=CLEAN) == "inf"
    assert normalize_value(float("nan"), mode=CLEAN) == "nan"


def test_numbers_of_every_type_meet_in_clean_mode() -> None:
    v = lambda x: normalize_value(x, mode=CLEAN)  # noqa: E731
    assert v(3) == v(3.0) == v(Decimal("3.001"))
    assert hash(v(3)) == hash(v(Decimal("3.00")))
    assert v(-0.001) == v(0)


def test_huge_and_non_finite_numbers_do_not_raise_in_clean_mode() -> None:
    v = lambda x: normalize_value(x, mode=CLEAN)  # noqa: E731
    assert v(Decimal("1" + "0" * 40 + ".555")) is not None
    assert v(float("nan")) == "nan"
    assert v(float("inf")) == "inf"
    assert v(Decimal("NaN")) == "NaN"


def test_dates_become_plain_dates_in_both_modes() -> None:
    for mode in (OFFICIAL, CLEAN):
        assert normalize_value(dt.date(2024, 3, 5), mode=mode) == "2024-03-05"
        assert (
            normalize_value(dt.datetime(2024, 3, 5, 13, 45, 1), mode=mode)
            == "2024-03-05"
        )


def test_nested_values_are_json_dumped_and_bool_stays_bool() -> None:
    for mode in (OFFICIAL, CLEAN):
        assert (
            normalize_value({"b": 1, "a": [1, 2]}, mode=mode) == '{"a": [1, 2], "b": 1}'
        )
        assert normalize_value([1, "x"], mode=mode) == '[1, "x"]'
        assert normalize_value(True, mode=mode) is True
        assert normalize_value(None, mode=mode) is None
        assert normalize_value("Text ", mode=mode) == "Text "  # not folded


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
        gold=(GoldAnswer("gold", GOLD),),
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


GOLD = "SELECT DISTINCT ROUND(x, 2) FROM t"
GOLD_SQL = normalize_sql(GOLD, mode=CLEAN)  # what clean mode runs: SELECT  (x) FROM t
GOLD_SQL_OFFICIAL = normalize_sql(GOLD)  # what official mode runs: SELECT x FROM t


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
    for mode, key in ((OFFICIAL, "SELECT x FROM t;"), (CLEAN, "SELECT  (x) FROM t")):
        db = FakeDb({key: [[1]]})
        out = score_sql(
            case,
            "SELECT DISTINCT ROUND(x, 2) FROM t;",
            GoldResult([[1]]),
            db,
            mode=mode,
        )
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
    score = score_report(report, cases, db, mode=CLEAN)
    tiers = score.tier_scores()
    assert tiers["Simple"] == (2, 0.5)
    assert tiers["Challenging"][0] == 1 and math.isclose(tiers["Challenging"][1], 2 / 3)
    assert tiers["Overall"][0] == 3
    assert list(tiers) == ["Simple", "Challenging", "Overall"]
    assert [c for _, c in db.calls].count(GOLD_SQL) == 3  # once per case, not per run
    assert score.question_passed("c") and not score.question_passed("b")


def test_score_report_accepts_plain_lowercase_tier_tags() -> None:
    case = replace(_case("a"), tags=("moderate", "gold-fixed"))
    db = FakeDb({GOLD_SQL: [[1]], "good": [[1]]})
    score = score_report(_report("e", {"a": ["good"]}), [case], db, mode=CLEAN)
    assert "Moderate" in score.tier_scores()


def test_score_report_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="not in the cases"):
        score_report(_report("e", {"zzz": ["x"]}), [_case("a")], FakeDb({}), mode=CLEAN)


def test_unusable_gold_scores_zero_and_is_reported() -> None:
    db = FakeDb({GOLD_SQL: RuntimeError("syntax error")})
    score = score_report(_report("e", {"a": ["x"]}), [_case("a")], db, mode=CLEAN)
    assert score.tier_scores()["Overall"] == (1, 0.0)
    assert "a" in score.gold_problems
    assert "gold unusable" in render_scores([score])


def test_score_report_matches_ids_with_or_without_the_lsb_prefix() -> None:
    db = FakeDb({GOLD_SQL: [[1]], "good": [[1]]})
    # frozen reports say "lsb.alien_1", lsb-build case files say "alien_1"
    one = score_report(
        _report("e", {"lsb.alien_1": ["good"]}), [_case("alien_1")], db, mode=CLEAN
    )
    assert one.question_passed("alien_1")
    two = score_report(
        _report("e", {"alien_1": ["good"]}), [_case("lsb.alien_1")], db, mode=CLEAN
    )
    assert two.question_passed("lsb.alien_1")
    # both spellings at once are ambiguous, not two questions
    with pytest.raises(ValueError, match="differ only by"):
        score_report(_report("e", {"a": ["good"]}), [_case("a"), _case("lsb.a")], db)


def test_score_report_falls_back_to_the_reports_target_db() -> None:
    """Frozen reports were run per database; their cases carry no database."""
    case = _case("a", db="")
    report = _report("e", {"a": ["good"]})
    report.meta["target_db"] = "museum"
    db = FakeDb({GOLD_SQL: [[1]], "good": [[1]]})
    score_report(report, [case], db, mode=CLEAN)
    assert [d for d, _ in db.calls] == ["museum", "museum"]


def test_score_report_refuses_a_case_with_no_database_anywhere() -> None:
    with pytest.raises(ValueError, match="no database"):
        score_report(
            _report("e", {"a": ["good"]}), [_case("a", db="")], FakeDb({}), mode=CLEAN
        )


def test_official_mode_breaks_a_gold_query_that_uses_is_distinct_from() -> None:
    """The official rewrite mangles IS DISTINCT FROM; the gold then scores 0
    for everyone, exactly as the benchmark's own harness would score it."""
    case = Case(
        id="a",
        question="q",
        gold=(GoldAnswer("gold", "SELECT 1 WHERE a IS DISTINCT FROM b"),),
        tags=("tier:Simple",),
        target_db="d1",
    )
    score = score_report(_report("e", {"a": ["SELECT 1"]}), [case], FakeDb({}))
    assert "a" in score.gold_problems
    assert score.tier_scores()["Overall"] == (1, 0.0)


def test_count_distinct_is_noise_in_official_mode_and_meaning_in_clean() -> None:
    case = Case(
        id="a",
        question="q",
        gold=(GoldAnswer("gold", "SELECT COUNT(DISTINCT x) FROM t"),),
        tags=("tier:Simple",),
        target_db="d1",
    )
    db = FakeDb(
        {
            "SELECT COUNT( x) FROM t": [[5]],  # what official mode runs
            "SELECT COUNT(DISTINCT x) FROM t": [[7]],  # what clean mode runs
            "SELECT COUNT(x) FROM t": [[5]],
        }
    )
    official = score_report(_report("e", {"a": ["SELECT COUNT(x) FROM t"]}), [case], db)
    assert official.question_passed("a")  # DISTINCT is stripped from both sides
    clean = score_report(
        _report("e", {"a": ["SELECT COUNT(x) FROM t"]}), [case], db, mode=CLEAN
    )
    assert not clean.question_passed("a")  # 5 against 7: the meaning differs


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
        mode=CLEAN,
    )
    two = score_report(
        _report("two", {"a": ["good"], "b": ["bad"], "c": ["bad"]}),
        cases,
        db,
        mode=CLEAN,
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


def test_cli_build_needs_kb_dir_only_with_oracle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    public, gt = tmp_path / "pub.jsonl", tmp_path / "gt.jsonl"
    public.write_text("\n".join(json.dumps(r) for r in PUBLIC[:2]) + "\n")
    gt.write_text("\n".join(json.dumps(r) for r in GT[:2]) + "\n")
    out = tmp_path / "out"
    # no --kb-dir, no --oracle: fine
    assert (
        cli.main(
            ["lsb-build", "--public", str(public), "--gt", str(gt), "--out", str(out)]
        )
        == 0
    )
    assert [p.name for p in out.iterdir()] == ["alpha.json"]
    # no --kb-dir but --oracle: refused with a message, not a crash
    code = cli.main(
        [
            "lsb-build",
            "--public",
            str(public),
            "--gt",
            str(gt),
            "--out",
            str(out),
            "--oracle",
        ]
    )
    assert code == 2
    assert "knowledge-base directory" in capsys.readouterr().err


# --------------------------------------------------- lsb-score against reports


class RecordingExecutor:
    """Stands in for PostgresExecutor in CLI tests: maps SQL to rows."""

    def __init__(
        self, dsn_template: str, timeout_s: float = 60.0, *, fetch_cap: int | None = 0
    ) -> None:
        assert "{db}" in dsn_template
        self.fetch_cap = fetch_cap
        self.results: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, database: str, sql: str) -> list[list[Any]]:
        self.calls.append((database, sql))
        outcome = self.results[sql]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        pass


def _cases_file(tmp_path: Path, *raw_cases: dict[str, Any]) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": list(raw_cases)}), encoding="utf-8")
    return path


COUNT_DISTINCT_CASE = {
    "id": "a",
    "question": "q",
    "tags": ["simple"],  # frozen reports use plain lowercase tier tags
    "gold": [{"label": "gold", "sql": "SELECT COUNT(DISTINCT x) FROM t"}],
}


def _lsb_report(tmp_path: Path, label: str, cid: str, sql: str) -> Path:
    report = Report(meta={"label": label, "target_db": "d1"})
    report.cases[cid] = CaseResult("q", [Run(False, "values", sql=sql, seconds=9.0)])
    path = tmp_path / f"{label}-{cid}.json"
    report.save(path)
    return path


def test_cli_lsb_score_official_by_default_clean_opt_in(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _cases_file(tmp_path, COUNT_DISTINCT_CASE)
    report = _lsb_report(tmp_path, "eng", "lsb.a", "SELECT COUNT(x) FROM t")
    executor = RecordingExecutor("postgresql://u:p@h/{db}")
    executor.results = {
        "SELECT COUNT( x) FROM t": [[5]],  # what official mode runs
        "SELECT COUNT(DISTINCT x) FROM t": [[7]],  # what clean mode runs
        "SELECT COUNT(x) FROM t": [[5]],
    }
    monkeypatch.setattr(cli, "PostgresExecutor", lambda *a, **k: executor)
    common = ["lsb-score", "--reports", str(report), "--cases", str(cases)]

    assert cli.main([*common, "--dsn-template", "postgresql://u:p@h/{db}"]) == 0
    out = capsys.readouterr().out
    assert "mode: official" in out and "100.0%" in out  # DISTINCT is noise
    assert all(db == "d1" for db, _ in executor.calls)  # meta.target_db fallback

    assert cli.main([*common, "--mode", "clean", "--dsn-template", "x/{db}"]) == 0
    out = capsys.readouterr().out
    assert "mode: clean" in out and "0.0%" in out  # 5 against 7: different meaning


def test_cli_lsb_score_refuses_a_report_measured_on_other_cases(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _cases_file(tmp_path, COUNT_DISTINCT_CASE)
    report = _lsb_report(tmp_path, "eng", "lsb.a", "SELECT COUNT(x) FROM t")
    loaded = Report.load(report)
    loaded.meta["cases_sha256"] = "0" * 64  # not the hash of these cases
    loaded.save(report)
    executor = RecordingExecutor("postgresql://u:p@h/{db}")
    monkeypatch.setattr(cli, "PostgresExecutor", lambda *a, **k: executor)
    common = [
        "lsb-score",
        "--reports",
        str(report),
        "--cases",
        str(cases),
        "--dsn-template",
        "postgresql://u:p@h/{db}",
    ]

    assert cli.main(common) == 2
    err = capsys.readouterr().err
    assert "not among --cases" in err and "--allow-cases-mismatch" in err
    assert not executor.calls  # nothing was scored
    assert cli.main([*common, "--allow-cases-mismatch"]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and executor.calls  # scored anyway, with a warning


def test_cli_lsb_score_merges_the_reports_that_share_a_label(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = dict(COUNT_DISTINCT_CASE, id="b")
    cases = _cases_file(tmp_path, COUNT_DISTINCT_CASE, other)
    one = _lsb_report(tmp_path, "eng", "lsb.a", "SELECT COUNT(x) FROM t")
    two = _lsb_report(tmp_path, "eng", "lsb.b", "SELECT COUNT(DISTINCT x) FROM t")
    executor = RecordingExecutor("postgresql://u:p@h/{db}")
    executor.results = {
        "SELECT COUNT( x) FROM t": [[5]],
        "SELECT COUNT(DISTINCT x) FROM t": [[7]],
        "SELECT COUNT(x) FROM t": [[5]],
    }
    monkeypatch.setattr(cli, "PostgresExecutor", lambda *a, **k: executor)
    code = cli.main(
        [
            "lsb-score",
            "--reports",
            str(one),
            str(two),
            "--cases",
            str(cases),
            "--dsn-template",
            "postgresql://u:p@h/{db}",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("eng") == 1 and "(2 questions)" in out

    # two same-label reports holding the same question are an error, not a merge
    clash = _lsb_report(tmp_path, "eng", "lsb.a", "SELECT COUNT(x) FROM t")
    code = cli.main(
        [
            "lsb-score",
            "--reports",
            str(one),
            str(clash),
            "--cases",
            str(cases),
            "--dsn-template",
            "postgresql://u:p@h/{db}",
        ]
    )
    assert code == 2
    assert "own --label" in capsys.readouterr().err


# ------------------------------------------------------- live Postgres executor


def _live_dsn_or_skip() -> str:
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("POSTGRES_* not set")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{{db}}"
    executor = PostgresExecutor(dsn)
    try:
        executor("postgres", "SELECT 1")
    except Exception as exc:  # noqa: BLE001 — any failure means "no fixture DB"
        pytest.skip(f"postgres not reachable: {exc}")
    finally:
        executor.close()
    return dsn


def test_live_executor_rejects_multi_statement_strings() -> None:
    """The COMMIT escape: read-only must not rely on the transaction alone."""
    executor = PostgresExecutor(_live_dsn_or_skip())
    try:
        with pytest.raises(ValueError, match="multiple statements"):
            executor("postgres", "SELECT 1; COMMIT; CREATE TABLE lsb_probe (v int)")
    finally:
        executor.close()


def test_live_executor_refuses_a_single_write_without_any_transaction() -> None:
    # read-only is set on the connection (default_transaction_read_only),
    # so a write fails even though no BEGIN was ever issued
    import psycopg

    executor = PostgresExecutor(_live_dsn_or_skip())
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            executor("postgres", "CREATE TABLE lsb_probe (v int)")
    finally:
        executor.close()


def test_live_executor_enforces_the_statement_timeout() -> None:
    import psycopg

    executor = PostgresExecutor(_live_dsn_or_skip(), 0.3)
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            executor("postgres", "SELECT pg_sleep(5)")
    finally:
        executor.close()


def test_live_executor_caps_the_rows_it_reads() -> None:
    dsn = _live_dsn_or_skip()
    executor = PostgresExecutor(dsn, fetch_cap=3)
    uncapped = PostgresExecutor(dsn, fetch_cap=None)
    try:
        assert len(executor("postgres", "SELECT * FROM generate_series(1, 7)")) == 3
        assert len(uncapped("postgres", "SELECT * FROM generate_series(1, 7)")) == 7
    finally:
        executor.close()
        uncapped.close()


def test_live_executor_reopens_a_dead_cached_connection() -> None:
    executor = PostgresExecutor(_live_dsn_or_skip())
    try:
        assert executor("postgres", "SELECT 1") == [[1]]
        executor._connections["postgres"].close()  # the server went away
        assert executor("postgres", "SELECT 2") == [[2]]
    finally:
        executor.close()
