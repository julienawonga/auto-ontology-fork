# SPDX-License-Identifier: Apache-2.0

"""lsb-kb against a fake backend and synthetic KB entries (no real benchmark data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from dev_tools.evals import __main__ as cli
from dev_tools.evals import lsb_kb

DB = "toy"
DB_ID = "db-1"
KB = [
    {
        "id": 0,
        "knowledge": "Widget Score",
        "description": "Scores a widget.",
        "definition": "$\\text{WS} = \\text{WidgetCount} \\times 2$",
        "type": "calculation_knowledge",
        "children_knowledge": -1,
    },
    {
        "id": 1,
        "knowledge": "Good Widget",
        "description": "A widget that scores high.",
        "definition": "WS > 10",
        "type": "domain_knowledge",
        "children_knowledge": [0],
    },
]


class FakeBackend:
    """Just enough of the Lexis API: catalog reads, model import/export, reindex."""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.terms: list[dict[str, Any]] = existing or []
        self.requests: list[tuple[str, str]] = []
        self.imports: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.reindexed: list[str] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        self.requests.append((method, path))
        if path == "/api/datasources/dbs":
            return httpx.Response(200, json={"data": [{"id": DB_ID, "name": DB}]})
        if path == f"/api/schemas/{DB_ID}":
            return httpx.Response(
                200, json={"schemas": [{"id": "s1", "schema_name": "public"}]}
            )
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
                200, json={"data": {"columns": [{"name": "WidgetCount"}]}}
            )
        if path == "/api/columns/t1":
            return httpx.Response(200, json={"data": {"columns": [{"name": "id"}]}})
        if path == "/api/terms":
            skip = int(req.url.params["skip"])
            limit = int(req.url.params["limit"])
            return httpx.Response(200, json={"terms": self.terms[skip : skip + limit]})
        if path == "/api/model/import":
            doc = yaml.safe_load(req.content)
            params = dict(req.url.params)
            self.imports.append((params, doc))
            wanted = doc["semantic_layer"]["terms"]
            if params["replace"] == "true":
                keep = {t["id"] for t in wanted}
                self.terms = [
                    t for t in self.terms if t.get("imported_id", t["id"]) in keep
                ]
                return httpx.Response(200, json={"summary": {"created": {"terms": 0}}})
            made = 0
            for t in wanted:
                if any(x.get("imported_id") == t["id"] for x in self.terms):
                    continue
                self.terms.append(
                    {
                        "id": f"u{len(self.terms)}",
                        "imported_id": t["id"],
                        "name": t["name"],
                        "description": t["description"],
                    }
                )
                made += 1
            return httpx.Response(200, json={"summary": {"created": {"terms": made}}})
        if path == "/api/model/export":
            return httpx.Response(
                200,
                text=yaml.safe_dump(
                    {
                        "data_layer": {"databases": []},
                        "semantic_layer": {
                            "terms": [
                                {
                                    "id": t.get("imported_id", t["id"]),
                                    "name": t["name"],
                                    "description": t["description"],
                                }
                                for t in self.terms
                            ]
                        },
                    }
                ),
            )
        if path == "/api/semantic-compilation/reindex":
            self.reindexed.append(req.url.params["database_name"])
            return httpx.Response(
                200, json={"database_name": DB, "expected": 1, "written": 1}
            )
        return httpx.Response(404, text="unexpected " + path)

    def api(self) -> lsb_kb.Api:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return lsb_kb.Api(client, "http://fake")

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    d = tmp_path / "kb" / DB
    d.mkdir(parents=True)
    (d / f"{DB}_kb.jsonl").write_text(
        "\n".join(json.dumps(e) for e in KB), encoding="utf-8"
    )
    return tmp_path / "kb"


def test_mapping(kb_dir: Path) -> None:
    kb = lsb_kb.check_db(DB, kb_dir)
    terms = lsb_kb.map_entries(kb, DB)
    assert [t.name for t in terms] == ["Widget Score", "Good Widget"]
    first, second = terms
    assert "Scores a widget." in first.description
    assert "\\text{WS}" in first.description  # formula kept as text
    assert "Depends on" not in first.description
    assert "Depends on: Widget Score" in second.description
    assert first.description.endswith("[lsb-kb:toy:0]")
    assert first.imported_id == "lsb-kb:toy:0"


def test_document_scopes_to_database_and_links_by_column(kb_dir: Path) -> None:
    fb = FakeBackend()
    api = fb.api()
    row = api.database(DB)
    schemas = api.tables(row["id"])
    for _, tbls in schemas:
        for t in tbls:
            t.columns = api.columns(t.id)
    terms = lsb_kb.map_entries(lsb_kb.check_db(DB, kb_dir), DB)
    doc = lsb_kb.build_document(DB, row, schemas, terms)
    assert doc["data_layer"]["databases"][0]["id"] == DB_ID
    reps = {t["name"]: t["represents"] for t in doc["semantic_layer"]["terms"]}
    assert reps["Widget Score"] == ["t2"]  # names column WidgetCount
    assert reps["Good Widget"] == ["t1"]  # no column named: first table by name


def test_dry_run_makes_no_write(kb_dir: Path) -> None:
    fb = FakeBackend()
    out = lsb_kb.dry_run(fb.api(), DB, lsb_kb.check_db(DB, kb_dir))
    assert out["would_create"] == 2 and len(out["first"]) == 2
    assert fb.writes() == []


def test_apply_is_idempotent_and_reindexes(kb_dir: Path) -> None:
    fb = FakeBackend()
    kb = lsb_kb.check_db(DB, kb_dir)
    first = lsb_kb.apply(fb.api(), DB, kb)
    assert first["created"] == 2 and first["present_after"] == 2
    assert not first["failures"] and first["reindex"]["ok"]
    params, _ = fb.imports[0]
    assert params == {"replace": "false", "embed": "true"}
    again = lsb_kb.apply(fb.api(), DB, kb)
    assert again["created"] == 0 and again["skipped_existing"] == 2
    assert len(fb.imports) == 1  # nothing new: no second import
    assert len(fb.terms) == 2
    assert fb.reindexed == [DB, DB]


def test_name_conflict_with_foreign_term_is_skipped(kb_dir: Path) -> None:
    fb = FakeBackend([{"id": "x", "name": "Widget Score", "description": "compiled"}])
    out = lsb_kb.apply(fb.api(), DB, lsb_kb.check_db(DB, kb_dir))
    assert out["name_conflicts"] == ["Widget Score"] and out["created"] == 1


def test_remove_deletes_only_ours(kb_dir: Path) -> None:
    other = {"id": "x", "name": "Compiled", "description": "a compiled term"}
    other_db = {
        "id": "y",
        "imported_id": "lsb-kb:other:3",
        "name": "Other",
        "description": "d\n[lsb-kb:other:3]",
    }
    fb = FakeBackend([other, other_db])
    kb = lsb_kb.check_db(DB, kb_dir)
    lsb_kb.apply(fb.api(), DB, kb)
    assert len(fb.terms) == 4
    out = lsb_kb.remove(fb.api(), DB)
    assert out["removed"] == 2 and not out["failures"]
    assert sorted(t["name"] for t in fb.terms) == ["Compiled", "Other"]
    params, _ = fb.imports[-1]
    assert params["replace"] == "true"
    assert fb.reindexed[-1] == DB


@pytest.mark.parametrize("db", ["pagila", "Chinook"])
def test_protected_databases_refused(kb_dir: Path, db: str) -> None:
    with pytest.raises(lsb_kb.KbToolError, match="protected"):
        lsb_kb.check_db(db, kb_dir)


def test_db_missing_from_kb_dir_refused(kb_dir: Path) -> None:
    with pytest.raises(lsb_kb.KbToolError, match="no knowledge base"):
        lsb_kb.check_db("nope", kb_dir)


def test_cli_refuses_protected_and_unknown(
    kb_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for db in ("pagila", "nope"):
        rc = cli.main(["lsb-kb", "--kb-dir", str(kb_dir), "--db", db, "--dry-run"])
        assert rc == 2
    assert "error" in capsys.readouterr().err


def test_cli_requires_a_mode(kb_dir: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["lsb-kb", "--kb-dir", str(kb_dir), "--db", DB])
