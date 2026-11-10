# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_tools.integrity.__main__ import REPO_ROOT, run_checks
from dev_tools.integrity.scan import (
    extract_needles,
    load_allowlist,
    normalise,
    scan_tree,
)

CASE = {
    "dataset": "toy",
    "cases": [
        {
            "id": "toy.best_band",
            "question": "Which band sold the most records in Lisbon?",
            "gold": [
                {
                    "label": "primary",
                    "sql": "SELECT band, 'Zebra Crossing' FROM sales WHERE city = 'Lisbon' "
                    "AND year = '2021' AND note = 'O''Neil Wharf'",
                    "expected": [["Velvet Anchors", 12, "n/a"], ["The", 3, None]],
                }
            ],
        }
    ],
}


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    cases = tmp_path / "dev_tools" / "evals" / "cases"
    cases.mkdir(parents=True)
    (cases / "toy.json").write_text(json.dumps(CASE), encoding="utf-8")
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tmp_path


def _scan(root: Path, allowlist: Path | None = None) -> list[str]:
    return [
        h.render() for h in scan_tree(root, root / "dev_tools/evals/cases", allowlist)
    ]


def test_normalise_ignores_case_punctuation_and_whitespace() -> None:
    assert normalise("  Which   band, sold?! ") == "which band sold"


def test_needles_keep_only_distinctive_values(tmp_path: Path) -> None:
    path = tmp_path / "toy.json"
    path.write_text(json.dumps(CASE), encoding="utf-8")
    needles = extract_needles(path)
    kinds = {(n.kind, n.norm) for n in needles}
    assert ("question", "which band sold the most records in lisbon") in kinds
    assert ("expected", "velvet anchors") in kinds
    assert ("literal", "zebra crossing") in kinds
    assert ("literal", "lisbon") in kinds
    assert ("literal", "o neil wharf") in kinds  # '' unescaped, punctuation dropped
    # too short, numeric, or null cells and literals are not distinctive
    assert not {n.norm for n in needles} & {"the", "12", "3", "n a", "2021"}


def test_a_clean_tree_has_no_hits(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"auto_ontology/mod.py": "print('hello world')\n"})
    assert _scan(root) == []


def test_a_question_in_a_prompt_fails_even_when_reworded_by_formatting(
    tmp_path: Path,
) -> None:
    prompt = 'EXAMPLE = """\nwhich BAND sold\n  the most records, in lisbon\n"""\n'
    root = _tree(tmp_path, {"auto_ontology/prompts.py": prompt})
    # "lisbon" is also a gold literal, so only the question hit is asserted on
    hits = [h for h in _scan(root) if ": question" in h]
    assert len(hits) == 1
    assert hits[0].startswith("auto_ontology/prompts.py:2: question")
    assert "toy.json" in hits[0] and "toy.best_band" in hits[0]


def test_an_expected_value_and_a_gold_literal_fail(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "auto_ontology/a.py": "ANSWER = 'Velvet Anchors'\n",
            "auto_ontology/b.json": '{"hint": "zebra crossing"}\n',
            "frontend/c.tsx": "const x = 'Lisbon'\n",
        },
    )
    hits = _scan(root)
    assert any("a.py:1: expected" in h for h in hits)
    assert any("b.json:1: literal" in h for h in hits)
    assert any("c.tsx:1: literal" in h for h in hits)


def test_tests_and_node_modules_are_not_production(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "auto_ontology/x/tests/test_a.py": "Velvet Anchors\n",
            "auto_ontology/x/test_b.py": "Velvet Anchors\n",
            "auto_ontology/conftest.py": "Velvet Anchors\n",
            "frontend/components/Foo.test.tsx": "Velvet Anchors\n",
            "frontend/node_modules/pkg/index.js": "Velvet Anchors\n",
        },
    )
    assert _scan(root) == []


def test_substrings_of_longer_words_do_not_match(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"auto_ontology/a.py": "x = 'Lisbonne'\n"})
    assert _scan(root) == []


def _allowlist(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_a_reviewed_allowlist_entry_silences_an_expected_value(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"auto_ontology/a.py": "ANSWER = 'Velvet Anchors'\n"})
    allow = _allowlist(
        tmp_path,
        [
            {
                "value": "velvet anchors",
                "reason": "A product name unrelated to the eval.",
            }
        ],
    )
    assert _scan(root, allow) == []


def test_an_allowlist_entry_can_be_scoped_to_a_path(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "auto_ontology/a.py": "ANSWER = 'Velvet Anchors'\n",
            "auto_ontology/b.py": "ANSWER = 'Velvet Anchors'\n",
        },
    )
    allow = _allowlist(
        tmp_path,
        [
            {
                "value": "Velvet Anchors",
                "path": "auto_ontology/a.py",
                "reason": "Only this file uses it, for something else.",
            }
        ],
    )
    hits = _scan(root, allow)
    assert len(hits) == 1 and hits[0].startswith("auto_ontology/b.py")


def test_a_question_cannot_be_allowlisted(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {"auto_ontology/a.py": "Which band sold the most records in Lisbon?\n"},
    )
    allow = _allowlist(
        tmp_path,
        [
            {
                "value": "Which band sold the most records in Lisbon?",
                "reason": "Trying to wave a question through anyway.",
            }
        ],
    )
    assert any(": question" in h for h in _scan(root, allow))


@pytest.mark.parametrize("reason", [None, "", "   ", "ok"])
def test_an_allowlist_entry_without_a_reason_is_rejected(
    tmp_path: Path, reason: str | None
) -> None:
    entry: dict = {"value": "Velvet Anchors"}
    if reason is not None:
        entry["reason"] = reason
    with pytest.raises(ValueError, match="written reason"):
        load_allowlist(_allowlist(tmp_path, [entry]))


def test_run_checks_fails_on_a_contaminated_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"auto_ontology/prompts.py": "Velvet Anchors\n"})
    problems = run_checks(root, allowlist=None)
    assert any("prompts.py:1" in p for p in problems)


def test_the_current_repository_passes() -> None:
    assert run_checks(REPO_ROOT) == []
