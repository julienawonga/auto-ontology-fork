# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from decimal import Decimal

from dev_tools.evals.compare import compare_any, compare_rows


def test_identical_rows_match() -> None:
    assert compare_rows([["a", 1]], [["a", 1]]).passed


def test_column_order_and_names_do_not_matter() -> None:
    assert compare_rows(
        [[22.97, "SEATTLE EXPECATIONS"]], [["SEATTLE EXPECATIONS", 22.97]]
    ).passed


def test_row_order_ignored_unless_ordered() -> None:
    actual = [["b", 2], ["a", 1]]
    expected = [["a", 1], ["b", 2]]
    assert compare_rows(actual, expected).passed
    verdict = compare_rows(actual, expected, ordered=True)
    assert not verdict.passed and verdict.reason == "order"


def test_case_and_whitespace_in_strings_ignored() -> None:
    assert compare_rows([["  RUTH   MARTINEZ "]], [["Ruth Martinez"]]).passed


def test_numbers_compared_to_four_significant_figures() -> None:
    assert compare_rows([[115.272]], [[115.3]]).passed
    assert compare_rows([[Decimal("2502.01")]], [[2502.0]]).passed
    assert not compare_rows([[115.9]], [[115.3]]).passed


def test_numeric_strings_are_numbers() -> None:
    # DataFrame.to_json(default_handler=str) turns Decimals into strings.
    assert compare_rows([["22.97"]], [[22.97]]).passed


def test_midnight_timestamp_equals_bare_date() -> None:
    assert compare_rows([["2022-05-24T00:00:00.000"]], [["2022-05-24"]]).passed
    assert not compare_rows([["2022-05-24T13:00:00"]], [["2022-05-24"]]).passed


def test_none_result_is_distinct_from_empty_result() -> None:
    assert compare_rows(None, [[1]]).reason == "no_result"
    assert compare_rows([], [[1]]).reason == "empty_result"
    assert compare_rows([], []).passed
    assert compare_rows([[1]], []).reason == "unexpected_rows"


def test_row_count_mismatch_fails() -> None:
    verdict = compare_rows([["a"], ["b"]], [["a"]])
    assert not verdict.passed and verdict.reason == "row_count"


def test_extra_column_fails_by_default_and_passes_when_allowed() -> None:
    actual = [["Drama", 104.79, 2]]
    expected = [["Drama", 104.79]]
    assert compare_rows(actual, expected).reason == "column_count"
    assert compare_rows(actual, expected, allow_extra_columns=True).passed


def test_extra_columns_still_need_the_expected_values() -> None:
    verdict = compare_rows(
        [["Travel", 96.79, 2]], [["Drama", 104.79]], allow_extra_columns=True
    )
    assert not verdict.passed and verdict.missing


def test_wrong_values_report_what_differs() -> None:
    verdict = compare_rows([["Documentary", 103.72]], [["Drama", 104.79]])
    assert verdict.reason == "values"
    # Cells are shown normalised and in sorted order, not in column order.
    assert [set(row) for row in verdict.missing] == [{"drama", 104.8}]
    assert [set(row) for row in verdict.unexpected] == [{"documentary", 103.7}]


def test_duplicate_rows_are_counted() -> None:
    assert not compare_rows([["a"], ["a"]], [["a"], ["b"]]).passed


def test_any_accepted_answer_passes_and_names_the_match() -> None:
    accepted = {
        "inventory_store": [["Drama", 104.79]],
        "customer_store": [["Documentary", 103.72]],
    }
    result = compare_any([["Documentary", 103.72]], accepted)
    assert result.passed and result.matched == "customer_store"

    miss = compare_any([["Sports", 95.0]], accepted)
    assert not miss.passed and miss.matched is None and miss.reason == "values"


def test_missing_columns_fail_by_default_and_pass_when_allowed() -> None:
    actual = [["Sports"]]
    expected = [["Sports", 74]]
    assert compare_rows(actual, expected).reason == "column_count"
    assert compare_rows(actual, expected, allow_missing_columns=True).passed


def test_missing_columns_still_need_the_right_values() -> None:
    verdict = compare_rows([["Travel"]], [["Sports", 74]], allow_missing_columns=True)
    assert not verdict.passed and verdict.reason == "values"
