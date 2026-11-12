# SPDX-License-Identifier: Apache-2.0

"""Load LiveSQLBench knowledge-base entries into Lexis as business Terms.

Arm C of the knowledge experiment: the same knowledge that arm B pastes into
the question as ``evidence`` is stored in the ontology and left for the engine
to retrieve. Everything goes through the backend's HTTP API, one database at a
time, the way a customer would:

* create: ``POST /api/model/import?replace=false`` with a small model document
  (the database's tables by live id, plus the Terms). There is no "create Term"
  route; the model import is the supported write path for Terms.
* delete: there is no "delete Term" route either. ``--remove`` exports the
  database (``POST /api/model/export``), drops our Terms from the document and
  imports it back with ``replace=true``, which deletes the Terms a document no
  longer names.
* index: ``POST /api/semantic-compilation/reindex?database_name=...``.

Our Terms are recognised by a marker line at the end of the description,
``[lsb-kb:<db>:<id>]``, because the Term API exposes no ``source`` or tag a
caller can set. The same string, without brackets, is the Term's ``imported_id``
so a repeated import matches instead of duplicating.

Nothing here reads the benchmark's gold. The KB entries are public; they are
never copied into the repository (tests use synthetic entries).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from dev_tools.evals.lsb import load_kb

#: Databases this tool refuses to touch, whatever the KB dir holds.
PROTECTED_DBS = frozenset({"pagila", "chinook"})
MARKER_PREFIX = "lsb-kb"
_MARKER_RE = re.compile(r"\[lsb-kb:([^:\]\s]+):([^\]\s]+)\]\s*$")
_PAGE = 100
#: A column name shorter than this is too common a word to link a Term by.
_MIN_COLUMN_LEN = 4


class KbToolError(Exception):
    """A refusal or a failure the CLI reports as an error (exit 2)."""


def marker(db: str, kb_id: str) -> str:
    return f"{MARKER_PREFIX}:{db}:{kb_id}"


def our_marker(description: str | None, db: str) -> str | None:
    """The ``imported_id`` of a description's marker when it is ours, else None."""
    match = _MARKER_RE.search(description or "")
    if match and match.group(1) == db:
        return f"{MARKER_PREFIX}:{db}:{match.group(2)}"
    return None


def check_db(db: str, kb_dir: Path) -> dict[str, dict[str, Any]]:
    """Guard, then load the KB: refuses protected databases and unknown ones."""
    if db.strip().lower() in PROTECTED_DBS:
        raise KbToolError(f"refusing to touch {db!r}: protected database")
    try:
        return load_kb(kb_dir, db)
    except FileNotFoundError as exc:
        raise KbToolError(str(exc)) from exc


@dataclass
class KbTerm:
    kb_id: str
    name: str
    description: str
    imported_id: str
    text: str  # the searchable text used to link tables (not sent)


def map_entries(kb: dict[str, dict[str, Any]], db: str) -> list[KbTerm]:
    """One Term per KB entry: name, description + formula + dependencies, marker."""
    names = {i: str(e["knowledge"]) for i, e in kb.items()}
    out: list[KbTerm] = []
    seen: set[str] = set()
    for kb_id, entry in kb.items():
        name = str(entry["knowledge"]).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts = [str(entry.get("description") or "").strip()]
        definition = str(entry.get("definition") or "").strip()
        if definition:
            parts.append(f"Definition: {definition}")
        children = entry.get("children_knowledge")
        if isinstance(children, list) and children:
            deps = [names.get(str(c), str(c)) for c in children]
            parts.append("Depends on: " + ", ".join(deps))
        body = "\n".join(p for p in parts if p)
        out.append(
            KbTerm(
                kb_id=kb_id,
                name=name,
                description=f"{body}\n[{marker(db, kb_id)}]",
                imported_id=marker(db, kb_id),
                text=body.lower(),
            )
        )
    return out


@dataclass
class Table:
    id: str
    name: str
    columns: list[str] = field(default_factory=list)


class Api:
    """The few backend calls this tool makes; every write is named."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self.client = client
        self.base = base_url.rstrip("/")
        self.writes: list[str] = []

    def _json(self, resp: httpx.Response, what: str) -> Any:
        if resp.status_code >= 400:
            raise KbToolError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._json(
            self.client.get(f"{self.base}{path}", params=params or None), f"GET {path}"
        )

    def post(self, path: str, what: str, **kwargs: Any) -> httpx.Response:
        self.writes.append(path)
        return self.client.post(f"{self.base}{path}", **kwargs)

    def database(self, db: str) -> dict[str, Any]:
        for row in self.get("/api/datasources/dbs")["data"]:
            if row["name"] == db:
                return row
        raise KbToolError(f"database {db!r} is not in the Lexis catalog")

    def tables(self, db_id: str) -> list[tuple[dict[str, Any], list[Table]]]:
        out = []
        for schema in (self.get(f"/api/schemas/{db_id}") or {}).get("schemas", []):
            rows = self.get(f"/api/tables/{schema['id']}")["data"]
            out.append((schema, [Table(id=r["id"], name=r["name"]) for r in rows]))
        return out

    def columns(self, table_id: str) -> list[str]:
        data = self.get(f"/api/columns/{table_id}")["data"]
        cols = data.get("columns") if isinstance(data, dict) else data
        return [str(c.get("name") or "") for c in cols or []]

    def terms(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            page = self.get("/api/terms", skip=len(out), limit=_PAGE)["terms"]
            out.extend(page)
            if len(page) < _PAGE:
                return out


def link_tables(term: KbTerm, tables: list[Table]) -> list[str]:
    """Tables a Term is attached to: those whose columns its text names.

    A Term only belongs to a database through the tables that represent it, and
    the SQL path adds a retrieved Term's tables to the prompt, so attaching
    every table would flood it. Falls back to the first table by name so the
    Term is still scoped to the database.
    """
    hits = []
    for table in tables:
        for col in table.columns:
            if len(col) >= _MIN_COLUMN_LEN and re.search(
                rf"(?<![a-z0-9_]){re.escape(col.lower())}(?![a-z0-9_])", term.text
            ):
                hits.append(table.id)
                break
    if hits:
        return hits
    return [min(tables, key=lambda t: t.name).id] if tables else []


def build_document(
    db: str,
    db_row: dict[str, Any],
    schemas: list[tuple[dict[str, Any], list[Table]]],
    terms: list[KbTerm],
) -> dict[str, Any]:
    """The skeleton model document: live ids only, so catalog rows are matched
    (never created or changed); only the Terms are new."""
    all_tables = [t for _, tbls in schemas for t in tbls]
    return {
        "data_layer": {
            "databases": [
                {
                    "id": db_row["id"],
                    "schemas": [
                        {
                            "id": s["id"],
                            "name": s.get("schema_name") or "",
                            "database_name": db,
                            "tables": [{"id": t.id, "name": t.name} for t in tbls],
                        }
                        for s, tbls in schemas
                    ],
                }
            ]
        },
        "semantic_layer": {
            "terms": [
                {
                    "id": t.imported_id,
                    "name": t.name,
                    "description": t.description,
                    "represents": link_tables(t, all_tables),
                }
                for t in terms
            ]
        },
    }


@dataclass
class Plan:
    db: str
    entries: int
    new: list[KbTerm]
    existing: list[str]  # names already stored as ours
    conflicts: list[str]  # names taken by a non-lsb-kb Term


def plan(
    db: str, terms: list[KbTerm], stored: list[dict[str, Any]], entries: int
) -> Plan:
    ours = {our_marker(t.get("description"), db) for t in stored} - {None}
    taken = {t["name"] for t in stored if our_marker(t.get("description"), db) is None}
    new, existing, conflicts = [], [], []
    for term in terms:
        if term.imported_id in ours:
            existing.append(term.name)
        elif term.name in taken:
            conflicts.append(term.name)
        else:
            new.append(term)
    return Plan(db, entries, new, existing, conflicts)


def _reindex(api: Api, db: str) -> dict[str, Any]:
    resp = api.post(
        "/api/semantic-compilation/reindex",
        "reindex",
        params={"database_name": db},
        timeout=600,
    )
    if resp.status_code >= 400:
        return {"ok": False, "status": resp.status_code, "detail": resp.text[:300]}
    return {"ok": True, **resp.json()}


def _import(api: Api, doc: dict[str, Any], *, replace: bool, embed: bool) -> Any:
    resp = api.post(
        "/api/model/import",
        "import",
        params={"replace": str(replace).lower(), "embed": str(embed).lower()},
        content=yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).encode(),
        headers={"content-type": "application/x-yaml"},
        timeout=600,
    )
    if resp.status_code >= 400:
        raise KbToolError(
            f"model import failed: HTTP {resp.status_code} {resp.text[:500]}"
        )
    return resp.json().get("summary")


def dry_run(api: Api, db: str, kb: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Reads only: what ``--apply`` would create."""
    api.database(db)
    p = plan(db, map_entries(kb, db), api.terms(), len(kb))
    return {
        "action": "dry-run",
        "database": db,
        "kb_entries": p.entries,
        "would_create": len(p.new),
        "already_present": len(p.existing),
        "name_conflicts": p.conflicts,
        "first": [{"name": t.name, "description": t.description} for t in p.new[:3]],
    }


def apply(api: Api, db: str, kb: dict[str, dict[str, Any]]) -> dict[str, Any]:
    db_row = api.database(db)
    schemas = api.tables(db_row["id"])
    for _, tbls in schemas:
        for t in tbls:
            t.columns = api.columns(t.id)
    p = plan(db, map_entries(kb, db), api.terms(), len(kb))
    result: dict[str, Any] = {
        "action": "apply",
        "database": db,
        "kb_entries": p.entries,
        "created": 0,
        "skipped_existing": len(p.existing),
        "name_conflicts": p.conflicts,
        "failures": [],
    }
    if p.new:
        summary = _import(
            api, build_document(db, db_row, schemas, p.new), replace=False, embed=True
        )
        result["created"] = (summary or {}).get("created", {}).get("terms", 0)
        result["import_summary"] = summary
    result["reindex"] = _reindex(api, db)
    stored = [
        t for t in api.terms() if our_marker(t.get("description"), db) is not None
    ]
    result["present_after"] = len(stored)
    if len(stored) < len(p.new) + len(p.existing):
        result["failures"].append("fewer lsb-kb Terms stored than expected")
    return result


def remove(api: Api, db: str) -> dict[str, Any]:
    """Delete exactly our Terms of *db* (export, drop ours, import with replace)."""
    db_row = api.database(db)
    resp = api.post(
        "/api/model/export", "export", json={"databases": [db_row["id"]]}, timeout=600
    )
    if resp.status_code >= 400:
        raise KbToolError(f"export failed: HTTP {resp.status_code} {resp.text[:300]}")
    doc = yaml.safe_load(resp.text)
    terms = doc["semantic_layer"]["terms"]
    keep = [t for t in terms if our_marker(t.get("description"), db) is None]
    doomed = len(terms) - len(keep)
    result: dict[str, Any] = {
        "action": "remove",
        "database": db,
        "removed": 0,
        "failures": [],
    }
    if doomed:
        doc["semantic_layer"]["terms"] = keep
        _import(api, doc, replace=True, embed=False)
        left = [
            t for t in api.terms() if our_marker(t.get("description"), db) is not None
        ]
        result["removed"] = doomed - len(left)
        if left:
            result["failures"].append(f"{len(left)} lsb-kb Term(s) still stored")
    result["reindex"] = _reindex(api, db)
    return result


def write_log(kb_dir: Path, db: str, result: dict[str, Any]) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = kb_dir / f"lsb-kb-{db}-{result['action']}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path
