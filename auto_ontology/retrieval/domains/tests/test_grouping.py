# SPDX-License-Identifier: Apache-2.0
"""Pure tests for business-domain grouping and the model fallback."""

import json
from typing import Any

import pytest

from auto_ontology.retrieval.domains import (
    GroupingError,
    build_prompt,
    group_terms,
    parse_grouping,
)


def test_build_prompt_compacts_term_descriptions() -> None:
    prompt = build_prompt(
        [
            {
                "id": "a",
                "name": "Account",
                "description": "x" * 140,
                "synonyms": ["Ledger"],
            }
        ],
        "fr",
    )
    assert '"description": "' + "x" * 120 + '"' in prompt
    assert "fr" in prompt
    assert "Ledger" in prompt
    # Business names, not numbered groups.
    assert "Group 1" not in prompt.replace("never generic names like Group 1", "")


def test_parse_drops_unknown_and_duplicate_ids_and_fills_other() -> None:
    raw = json.dumps(
        {
            "domains": [
                {
                    "name": " Finance ",
                    "description": "Money",
                    "term_ids": ["a", "a", "unknown", "b"],
                },
                {"name": "Sales", "term_ids": ["c"]},
            ]
        }
    )
    parsed = parse_grouping(raw, {"a", "b", "c"})
    # The duplicated id keeps its first assignment, the unknown id is dropped,
    # the unassigned Term lands in Other, and the one-Term Sales domain merges
    # into Other (fewer than 2 Terms). Finance keeps its two Terms.
    assert parsed == [
        {"name": "Finance", "description": "Money", "term_ids": ["a", "b"]},
        {"name": "Other", "description": None, "term_ids": ["c"]},
    ]


def test_parse_too_few_terms_merges_everything_into_other() -> None:
    raw = json.dumps({"domains": [{"name": "Solo", "term_ids": ["a"]}]})
    parsed = parse_grouping(raw, {"a"})
    assert parsed == [{"name": "Other", "description": None, "term_ids": ["a"]}]


def test_parse_merges_duplicate_names() -> None:
    raw = json.dumps(
        {
            "domains": [
                {"name": "finance", "description": "First", "term_ids": ["a", "b"]},
                {"name": "Finance", "description": "Second", "term_ids": ["c", "d"]},
            ]
        }
    )
    parsed = parse_grouping(raw, {"a", "b", "c", "d"})
    assert parsed == [
        {
            "name": "finance",
            "description": "First",
            "term_ids": ["a", "b", "c", "d"],
        }
    ]


def test_parse_caps_at_eight_domains_keeping_all_terms() -> None:
    domains = [
        {"name": f"Domain {i}", "term_ids": [f"t{i}", f"u{i}"]} for i in range(9)
    ]
    valid = {item for i in range(9) for item in (f"t{i}", f"u{i}")}
    parsed = parse_grouping(json.dumps({"domains": domains}), valid)
    assert len(parsed) == 8
    assert {term for group in parsed for term in group["term_ids"]} == valid


def test_parse_unreadable_output_raises_typed_error() -> None:
    with pytest.raises(GroupingError):
        parse_grouping("not json", {"a"})
    with pytest.raises(GroupingError):
        parse_grouping(json.dumps({"no_domains": []}), {"a"})


class GarbageLLM:
    """A client that always answers something unparsable."""

    def bind(self, **_: Any) -> "GarbageLLM":
        return self

    def invoke(self, _: Any) -> Any:
        return type("Reply", (), {"content": "garbage"})()


def test_group_terms_retries_then_falls_back_to_cluster_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One retry then the fallback: the fallback names domains after the most
    # connected Term, which with no links is the name-sorted first Term.
    warnings: list[str] = []
    monkeypatch.setattr(
        "auto_ontology.retrieval.domains.logger.warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )
    result = group_terms(
        [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}], "en", GarbageLLM()
    )
    assert result == [{"name": "A", "description": None, "term_ids": ["a", "b"]}]
    assert any("fallback" in warning for warning in warnings)


def test_group_terms_empty_input_returns_empty() -> None:
    assert group_terms([], "en", GarbageLLM()) == []


def test_parse_accepts_json_inside_a_code_fence_and_prose() -> None:
    body = json.dumps(
        {"domains": [{"name": "Finance", "description": "", "term_ids": ["a", "b"]}]}
    )
    reply = f"Here is the grouping:\n```json\n{body}\n```\nHope this helps."
    assert [d["name"] for d in parse_grouping(reply, {"a", "b"})] == ["Finance"]
