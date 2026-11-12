# SPDX-License-Identifier: Apache-2.0

"""Experiment E tooling against an invented wiki, a fake LLM and a fake backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from dev_tools.evals import __main__ as cli
from dev_tools.evals import wiki, wiki_extract, wiki_kb

DB = "toy"
DB_ID = "db-1"

WIKI = """# Toy Handbook

Intro text before any section.

## 1 Gadgets

Gadget scoring uses the **Gizmo Index (GI)**.

$$
GI = \\frac{\\text{CogCount}}{\\text{Teeth}} \\times 100
$$

A gadget is **Shiny** when GI > 50.

## 2 Cogs

### 2.1 Wear

**Wear Rate (WR)** measures cog wear per hour.

## 3 Empty parent

### 3.1 Same

First same.

### 3.1 Same

Second same.
"""


def test_chunking_is_deterministic_and_keeps_math_whole() -> None:
    a = wiki.chunk_markdown(WIKI)
    b = wiki.chunk_markdown(WIKI)
    assert [c.id for c in a] == [c.id for c in b]
    assert len({c.id for c in a}) == len(a)
    by_path = {c.heading: c for c in a}
    assert a[0].id == "preamble" or a[0].path == ("Toy Handbook",)
    gadgets = by_path["Toy Handbook > 1 Gadgets"]
    assert gadgets.text.count("$$") == 2  # the block stays in its section
    assert "\\frac{\\text{CogCount}}{\\text{Teeth}}" in gadgets.text
    assert "Toy Handbook > 2 Cogs" not in by_path  # no body of its own
    assert "Toy Handbook > 2 Cogs > 2.1 Wear" in by_path
    same = [c for c in a if c.heading.endswith("3.1 Same")]
    assert [c.text for c in same] == ["First same.", "Second same."]
    assert same[0].id != same[1].id


def test_heading_inside_math_is_not_a_heading() -> None:
    text = "## A\n\n$$\n# x\n$$\n\ntail\n"
    chunks = wiki.chunk_markdown(text)
    assert len(chunks) == 1 and "# x" in chunks[0].text


def test_long_section_splits_between_paragraphs_never_inside_math() -> None:
    para = "word " * 60
    text = f"## A\n\n{para}\n\n$$\nx = 1\n\ny = 2\n$$\n\n{para}\n"
    chunks = wiki.chunk_markdown(text, max_chars=400)
    assert len(chunks) >= 2
    assert all(c.text.count("$$") % 2 == 0 for c in chunks)
    assert chunks[0].id.endswith(".p1")


CHUNKS = [
    wiki.Chunk("c1", ("Boats",), "Sailing boats use a keel and a mast."),
    wiki.Chunk("c2", ("Cars",), "Cars use an engine; engine torque matters most."),
    wiki.Chunk("c3", ("Planes",), "Planes use wings and an engine."),
]


def test_bm25_ranks_the_relevant_chunk_first_and_is_stable() -> None:
    bm = wiki.Bm25()
    top = bm.rank("how much engine torque", CHUNKS, 2)
    assert [c.id for c, _ in top] == ["c2", "c3"]
    assert bm.rank("how much engine torque", CHUNKS, 2) == top
    assert bm.rank("zzz unknown", CHUNKS, 2) == []  # nothing matches: no evidence


def test_embedding_similarity_uses_the_injected_function() -> None:
    vecs = {"q": [1.0, 0.0]}

    def embed(texts: list[str], kind: str) -> list[list[float] | None]:
        if kind == "query":
            return [vecs["q"]]
        return [[0.0, 1.0], [1.0, 0.1], [0.5, 0.5]]

    top = wiki.EmbeddingSimilarity(embed).rank("q", CHUNKS, 1)
    assert top[0][0].id == "c2"


def test_rag_cases_replaces_evidence_in_a_copy() -> None:
    payload = {
        "source": "x",
        "cases": [
            {
                "id": "q1",
                "question": "engine torque?",
                "gold": [{"label": "a", "sql": "select 1"}],
                "evidence": "old",
            }
        ],
    }
    before = json.dumps(payload)
    new, summary = wiki.rag_cases(payload, CHUNKS, wiki.Bm25(), k=1, wiki_sha256="h")
    assert json.dumps(payload) == before  # input untouched
    case = new["cases"][0]
    assert case["evidence"].startswith("[Cars]") and "Boats" not in case["evidence"]
    assert case["gold"] == payload["cases"][0]["gold"]
    assert new["wiki_rag"]["k"] == 1 and summary["evidence_replaced"] == 1
    # a budget drops whole chunks instead of cutting them
    tiny, _ = wiki.rag_cases(payload, CHUNKS, wiki.Bm25(), k=3, max_chars=5)
    assert tiny["cases"][0]["evidence"] == ""


def test_cli_rag_refuses_output_in_repo_and_input_overwrite(tmp_path: Path) -> None:
    w = tmp_path / "w.md"
    w.write_text("## A\n\nengine\n", encoding="utf-8")
    cases = tmp_path / "c.json"
    cases.write_text(json.dumps({"cases": []}), encoding="utf-8")
    assert (
        cli.main(
            ["wiki-rag", "--wiki", str(w), "--cases", str(cases), "--out", str(cases)]
        )
        == 2
    )
    inside = cli.REPO_ROOT / "dev_tools" / "evals" / "should_not_exist.json"
    assert (
        cli.main(
            ["wiki-rag", "--wiki", str(w), "--cases", str(cases), "--out", str(inside)]
        )
        == 2
    )
    assert not inside.exists()
    out = tmp_path / "o.json"
    assert (
        cli.main(
            ["wiki-rag", "--wiki", str(w), "--cases", str(cases), "--out", str(out)]
        )
        == 0
    )
    assert out.exists()


# ------------------------------ extraction -------------------------------- #

CHUNK = wiki.Chunk(
    "k1",
    ("Toy", "Gadgets"),
    "The Gizmo Index (GI) is computed as\n$$\nGI = \\text{CogCount} / \\text{Teeth}\n$$\n"
    "A gadget is Shiny when GI > 50.",
)


def _term(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Gizmo Index",
        "abbreviation": "GI",
        "definition": "Cogs per tooth.",
        "formula": "GI = \\text{CogCount} / \\text{Teeth}",
        "thresholds": [],
        "depends_on": [],
        "candidate_columns": ["CogCount"],
        "quote": "The Gizmo Index (GI) is computed as",
    }
    base.update(kw)
    return base


def test_quote_validation_rejects_paraphrase_and_bad_formula() -> None:
    good, bad = wiki_extract.validate_terms(
        CHUNK,
        [
            _term(),
            _term(name="Para", quote="The gizmo index is computed as"),  # paraphrase
            _term(name="BadF", formula="GI = CogCount / Teeth"),
            {"name": "NoQuote", "definition": "x"},
        ],
    )
    assert [t.name for t in good] == ["Gizmo Index"]
    reasons = {r.name: r.reason for r in bad}
    assert "quote" in reasons["Para"] and "formula" in reasons["BadF"]
    assert "missing" in reasons["NoQuote"]


def test_latex_backslashes_eaten_by_json_are_recovered() -> None:
    # A model that writes "\text" unescaped yields TAB + "ext" after json.loads.
    reply = (
        '{"terms": [{"name": "Gizmo Index", "abbreviation": null, "definition": "d",'
        ' "formula": "GI = \\text{CogCount} / \\text{Teeth}", "thresholds": [],'
        ' "depends_on": [], "candidate_columns": [], "quote": "GI > 50"}]}'
    )
    good, bad = wiki_extract.validate_terms(CHUNK, wiki_extract.parse_reply(reply))
    assert not bad and good[0].formula == "GI = \\text{CogCount} / \\text{Teeth}"


def test_parse_reply_tolerates_fences_and_retries_once() -> None:
    replies = iter(["not json", '```json\n{"terms": []}\n```'])
    terms, rejects = wiki_extract.extract_chunk(CHUNK, lambda s, u: next(replies))
    assert terms == [] and rejects == []
    terms, rejects = wiki_extract.extract_chunk(CHUNK, lambda s, u: "still bad")
    assert terms == [] and "chunk failed" in rejects[0].reason


def _raw(name: str, chunk: str, **kw: Any) -> wiki_extract.RawTerm:
    base: dict[str, Any] = {
        "name": name,
        "abbreviation": "",
        "definition": "d",
        "formula": "",
        "thresholds": [],
        "depends_on": [],
        "candidate_columns": [],
        "chunk_id": chunk,
        "heading_path": ["H"],
        "quote": "q",
    }
    base.update(kw)
    return wiki_extract.RawTerm(**base)


def test_consolidation_dedupes_merges_and_flags() -> None:
    out = wiki_extract.consolidate(
        [
            _raw("Gizmo Index", "a", abbreviation="GI", formula="GI = x / y"),
            _raw(
                "gizmo  index", "b", definition="longer definition", thresholds=["> 50"]
            ),
            _raw("GI", "c", formula="GI = x / y"),  # abbreviation of the same term
            _raw("Wear Rate", "a", formula="WR = a"),
            _raw("Wear Rate", "b", formula="WR = b"),  # contradiction
            _raw("Shiny", "a", depends_on=["Gizmo Index", "Mystery Metric"]),
        ]
    )
    by = {t["name"]: t for t in out}
    assert list(by) == ["Gizmo Index", "Wear Rate", "Shiny"]
    gi = by["Gizmo Index"]
    assert len(gi["sources"]) == 3 and gi["definition"] == "longer definition"
    assert gi["thresholds"] == ["> 50"] and gi["flags"] == []
    wr = by["Wear Rate"]
    assert "contradiction" in wr["flags"] and wr["variants"][0]["formula"] == "WR = b"
    assert wr["formula"] == "WR = a"  # first source stays primary
    shiny = by["Shiny"]
    assert shiny["flags"] == ["undefined_dependency"]
    assert shiny["undefined_dependencies"] == ["Mystery Metric"]
    assert all(t["include"] is True for t in out)


def test_extract_all_end_to_end_with_fake_llm() -> None:
    chunks = [CHUNK, wiki.Chunk("k2", ("Toy",), "Nothing here.")]

    def llm(system: str, user: str) -> str:
        if "Nothing here" in user:
            return '{"terms": []}'
        return json.dumps({"terms": [_term(), _term(name="Bad", quote="made up")]})

    raw, rejects = wiki_extract.extract_all(chunks, llm, workers=2)
    assert [t.name for t in raw] == ["Gizmo Index"] and len(rejects) == 1
    doc = wiki_extract.build_document(DB, "sha", wiki_extract.consolidate(raw), rejects)
    again = yaml.safe_load(yaml.safe_dump(doc, sort_keys=False))
    assert again["terms"][0]["sources"][0]["chunk_id"] == "k1"
    assert again["rejected"][0]["name"] == "Bad"


# -------------------------------- loading --------------------------------- #

LSB_DESC = "Foreign.\n[lsb-kb:toy:7]"


class FakeBackend:
    """Catalog reads, Terms create/delete, reindex."""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.terms: list[dict[str, Any]] = list(existing or [])
        self.requests: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.reindexed: list[str] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        self.requests.append((method, path))
        if path == "/api/datasources/dbs":
            return httpx.Response(200, json={"data": [{"id": DB_ID, "name": DB}]})
        if path == f"/api/schemas/{DB_ID}":
            return httpx.Response(200, json={"schemas": [{"id": "s1"}]})
        if path == "/api/tables/s1":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "t2", "name": "zeta"},
                        {"id": "t1", "name": "alpha"},
                    ]
                },
            )
        if path == "/api/columns/t2":
            return httpx.Response(
                200, json={"data": {"columns": [{"name": "CogCount"}]}}
            )
        if path == "/api/columns/t1":
            return httpx.Response(200, json={"data": {"columns": [{"name": "id"}]}})
        if path == "/api/terms" and method == "GET":
            skip, limit = int(req.url.params["skip"]), int(req.url.params["limit"])
            return httpx.Response(200, json={"terms": self.terms[skip : skip + limit]})
        if path == "/api/terms" and method == "POST":
            body = json.loads(req.content)
            if any(t["name"] == body["name"] for t in self.terms):
                return httpx.Response(409, text="taken")
            self.created.append(body)
            self.terms.append(
                {
                    "id": f"w{len(self.terms)}",
                    "name": body["name"],
                    "description": body["description"],
                }
            )
            return httpx.Response(201, json={"data": {"id": "x", "embedded": True}})
        if path.startswith("/api/terms/") and method == "DELETE":
            tid = path.rsplit("/", 1)[1]
            self.terms = [t for t in self.terms if t["id"] != tid]
            return httpx.Response(200, json={"data": {"id": tid}})
        if path == "/api/semantic-compilation/reindex":
            self.reindexed.append(req.url.params["database_name"])
            return httpx.Response(200, json={"expected": 1, "written": 1})
        return httpx.Response(404, text="unexpected " + path)

    def api(self) -> wiki_kb.WikiApi:
        return wiki_kb.WikiApi(
            httpx.Client(transport=httpx.MockTransport(self.handle)), "http://fake"
        )

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]


DOC = {
    "terms": [
        {
            "id": "gizmo-index",
            "name": "Gizmo Index",
            "abbreviation": "GI",
            "definition": "Cogs per tooth.",
            "formula": "GI = x / y",
            "thresholds": ["> 50 is high"],
            "depends_on": ["Teeth"],
            "candidate_columns": ["CogCount"],
            "include": True,
        },
        {
            "id": "shiny",
            "name": "Shiny",
            "definition": "A good gadget.",
            "include": True,
        },
        {"id": "dropped", "name": "Dropped", "definition": "x", "include": False},
        {"id": "taken", "name": "Taken Name", "definition": "x", "include": True},
    ]
}


def _existing() -> list[dict[str, Any]]:
    return [
        {"id": "L1", "name": "Taken Name", "description": LSB_DESC},
        {"id": "L2", "name": "Other", "description": LSB_DESC.replace("7", "8")},
    ]


def test_mapping_marker_synonyms_and_review_exclusion() -> None:
    terms, skipped = wiki_kb.map_terms(DOC, DB)
    assert [t.name for t in terms] == ["Gizmo Index", "Shiny", "Taken Name"]
    assert skipped == ["Dropped"]
    gi = terms[0]
    assert gi.description.endswith("[wiki-e:toy:gizmo-index]")
    assert "Formula: GI = x / y" in gi.description and gi.synonyms == ["GI"]
    assert wiki_kb.our_marker(gi.description, DB) == "gizmo-index"
    assert wiki_kb.our_marker(gi.description, "other") is None
    assert wiki_kb.our_marker(LSB_DESC, DB) is None


def test_dry_run_writes_nothing_and_reports_collision() -> None:
    fb = FakeBackend(_existing())
    out = wiki_kb.dry_run(fb.api(), DB, DOC)
    assert out["would_create"] == 2 and fb.writes() == []
    assert out["name_collisions"] == [{"name": "Taken Name", "held_by": "lsb-kb"}]
    assert out["links"]["Gizmo Index"] == ["t2"]  # names column CogCount
    assert out["links"]["Shiny"] == ["t1"]  # fallback: first table by name


def test_apply_creates_through_terms_api_and_never_touches_foreign_terms() -> None:
    fb = FakeBackend(_existing())
    out = wiki_kb.apply(fb.api(), DB, DOC)
    assert out["created"] == 2 and not out["failures"] and out["present_after"] == 2
    assert out["name_collisions"] == [{"name": "Taken Name", "held_by": "lsb-kb"}]
    assert {m for m, _ in fb.writes()} == {"POST"}
    assert fb.created[0]["database"] == DB and fb.created[0]["table_ids"] == ["t2"]
    assert fb.created[0]["synonyms"] == ["GI"]
    assert [t["id"] for t in fb.terms[:2]] == ["L1", "L2"]
    assert fb.reindexed == [DB]
    again = wiki_kb.apply(fb.api(), DB, DOC)  # idempotent
    assert again["created"] == 0 and again["skipped_existing"] == 2
    assert len(fb.terms) == 4


def test_remove_deletes_only_our_marked_terms() -> None:
    fb = FakeBackend(_existing())
    wiki_kb.apply(fb.api(), DB, DOC)
    out = wiki_kb.remove(fb.api(), DB)
    assert out["removed"] == 2 and not out["failures"]
    assert [t["id"] for t in fb.terms] == ["L1", "L2"]
    assert fb.reindexed == [DB, DB]


def test_protected_database_refused() -> None:
    with pytest.raises(wiki_kb.KbToolError):
        wiki_kb.check_db("pagila")


# -------------------------------- scoring --------------------------------- #

KB = {
    "0": {
        "knowledge": "Gizmo Index (GI)",
        "description": "Cogs per tooth",
        "definition": "GI = x / 100",
    },
    "1": {"knowledge": "Shiny", "description": "good", "definition": ""},
    "2": {"knowledge": "Missing Metric", "description": "gone", "definition": ""},
}


def test_score_extraction_recall_missing_and_eyeball_list() -> None:
    doc = {
        "terms": [
            {
                "name": "Gizmo Index",
                "abbreviation": "GI",
                "definition": "Cogs per tooth",
                "formula": "GI = x / 10",
            },
            {"name": "shiny", "definition": "good"},
            {"name": "Extra Thing", "definition": "z"},
        ]
    }
    res = wiki_kb.score_extraction(doc, KB)
    assert res["matched"] == 2 and res["kb_terms"] == 3
    assert res["recall"] == pytest.approx(0.667, abs=1e-3)
    assert res["missing"] == ["Missing Metric"]
    assert res["extracted_not_in_kb"] == ["Extra Thing"]
    first = res["to_eyeball"][0]
    assert first["kb_name"] == "Gizmo Index (GI)"  # least similar first
    assert first["numbers_only_in_kb"] == ["100"]
    assert first["numbers_only_in_extraction"] == ["10"]
