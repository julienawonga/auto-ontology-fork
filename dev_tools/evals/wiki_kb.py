# SPDX-License-Identifier: Apache-2.0

"""Experiment E: store a reviewed wiki extraction as Terms, and score it.

``wiki-load`` goes through the Terms API (``POST /api/terms``,
``DELETE /api/terms/{id}``), the path a product uses, never the database. Our
Terms carry a marker line ``[wiki-e:<db>:<id>]`` at the end of the description
(the API has no tag), distinct from the LiveSQLBench loader's ``[lsb-kb:...]``:
this tool never edits or deletes a Term without its own marker, and a name that
another Term already holds is reported as a collision, not overwritten.

``wiki-score-extraction`` compares an extraction with the official knowledge
entries of the database (read with the LiveSQLBench KB reader, kept outside the
repository) by normalised name.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from dev_tools.evals import lsb_kb
from dev_tools.evals.lsb_kb import KbToolError, Table
from dev_tools.evals.wiki_extract import norm_name

MARKER_PREFIX = "wiki-e"
_MARKER_RE = re.compile(r"\[wiki-e:([^:\]\s]+):([^\]\s]+)\]\s*$")


def marker(db: str, term_id: str) -> str:
    return f"{MARKER_PREFIX}:{db}:{term_id}"


def our_marker(description: str | None, db: str) -> str | None:
    """The marker id of a description when it is ours for *db*, else None."""
    match = _MARKER_RE.search(description or "")
    if match and match.group(1) == db:
        return match.group(2)
    return None


def load_extraction(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("terms"), list):
        raise KbToolError(f"{path}: not a wiki-extraction file (no 'terms' list)")
    return doc


@dataclass
class WikiTerm:
    id: str
    name: str
    description: str
    synonyms: list[str]
    text: str  # lowercased text used to link tables (not sent)


def map_terms(doc: dict[str, Any], db: str) -> tuple[list[WikiTerm], list[str]]:
    """(Terms to store, names skipped by review). Only ``include: true`` entries."""
    out: list[WikiTerm] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in doc["terms"]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if entry.get("include") is False:
            skipped.append(name)
            continue
        tid = str(entry.get("id") or norm_name(name).replace(" ", "-"))
        if norm_name(name) in seen:
            skipped.append(name)
            continue
        seen.add(norm_name(name))
        parts = [str(entry.get("definition") or "").strip()]
        if entry.get("formula"):
            parts.append(f"Formula: {entry['formula']}")
        if entry.get("thresholds"):
            parts.append("Thresholds: " + "; ".join(map(str, entry["thresholds"])))
        if entry.get("depends_on"):
            parts.append("Depends on: " + ", ".join(map(str, entry["depends_on"])))
        body = "\n".join(p for p in parts if p)
        cols = " ".join(map(str, entry.get("candidate_columns") or []))
        abbr = str(entry.get("abbreviation") or "").strip()
        out.append(
            WikiTerm(
                id=tid,
                name=name,
                description=f"{body}\n[{marker(db, tid)}]",
                synonyms=[abbr] if abbr else [],
                text=f"{body} {cols}".lower(),
            )
        )
    return out, skipped


class WikiApi(lsb_kb.Api):
    """The lsb-kb API helper plus Term create and delete; every write is named."""

    def create_term(self, body: dict[str, Any]) -> httpx.Response:
        self.writes.append("POST /api/terms")
        return self.client.post(f"{self.base}/api/terms", json=body, timeout=120)

    def delete_term(self, term_id: str) -> httpx.Response:
        self.writes.append("DELETE /api/terms/{id}")
        return self.client.delete(f"{self.base}/api/terms/{term_id}", timeout=120)


def check_db(db: str) -> None:
    if db.strip().lower() in lsb_kb.PROTECTED_DBS:
        raise KbToolError(f"refusing to touch {db!r}: protected database")


def _owner(description: str | None) -> str:
    return "lsb-kb" if lsb_kb._MARKER_RE.search(description or "") else "other"


@dataclass
class Plan:
    new: list[WikiTerm]
    existing: list[str]
    collisions: list[dict[str, str]]


def plan(db: str, terms: list[WikiTerm], stored: list[dict[str, Any]]) -> Plan:
    ours = {
        our_marker(t.get("description"), db): t
        for t in stored
        if our_marker(t.get("description"), db)
    }
    taken = {
        str(t["name"]).casefold(): _owner(t.get("description"))
        for t in stored
        if not our_marker(t.get("description"), db)
    }
    new: list[WikiTerm] = []
    existing: list[str] = []
    collisions: list[dict[str, str]] = []
    for term in terms:
        if term.id in ours:
            existing.append(term.name)
        elif term.name.casefold() in taken:
            collisions.append(
                {"name": term.name, "held_by": taken[term.name.casefold()]}
            )
        else:
            new.append(term)
    return Plan(new, existing, collisions)


def _tables_with_columns(api: WikiApi, db_id: str) -> list[Table]:
    tables = [t for _, tbls in api.tables(db_id) for t in tbls]
    for t in tables:
        t.columns = api.columns(t.id)
    return tables


def _link(term: WikiTerm, tables: list[Table]) -> list[str]:
    return lsb_kb.link_tables(term, tables)  # type: ignore[arg-type]


def dry_run(api: WikiApi, db: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Reads only: what ``--apply`` would create."""
    db_row = api.database(db)
    terms, skipped = map_terms(doc, db)
    p = plan(db, terms, api.terms())
    tables = _tables_with_columns(api, db_row["id"])
    return {
        "action": "dry-run",
        "database": db,
        "in_file": len(doc["terms"]),
        "excluded_by_review": skipped,
        "would_create": len(p.new),
        "already_present": len(p.existing),
        "name_collisions": p.collisions,
        "links": {t.name: _link(t, tables) for t in p.new},
        "first": [{"name": t.name, "description": t.description} for t in p.new[:3]],
    }


def apply(api: WikiApi, db: str, doc: dict[str, Any]) -> dict[str, Any]:
    db_row = api.database(db)
    terms, skipped = map_terms(doc, db)
    p = plan(db, terms, api.terms())
    tables = _tables_with_columns(api, db_row["id"])
    result: dict[str, Any] = {
        "action": "apply",
        "database": db,
        "in_file": len(doc["terms"]),
        "excluded_by_review": skipped,
        "created": 0,
        "skipped_existing": len(p.existing),
        "name_collisions": p.collisions,
        "not_embedded": [],
        "failures": [],
    }
    for term in p.new:
        body: dict[str, Any] = {
            "name": term.name,
            "description": term.description,
            "database": db,
            "table_ids": _link(term, tables),
        }
        if term.synonyms:
            body["synonyms"] = term.synonyms
        resp = api.create_term(body)
        if resp.status_code == 409:
            result["name_collisions"].append({"name": term.name, "held_by": "other"})
        elif resp.status_code >= 400:
            result["failures"].append(
                f"{term.name}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        else:
            result["created"] += 1
            if not (resp.json().get("data") or {}).get("embedded", True):
                result["not_embedded"].append(term.name)
    result["reindex"] = lsb_kb._reindex(api, db)
    stored = [t for t in api.terms() if our_marker(t.get("description"), db)]
    result["present_after"] = len(stored)
    if len(stored) < result["created"] + len(p.existing):
        result["failures"].append("fewer wiki-e Terms stored than expected")
    return result


def remove(api: WikiApi, db: str) -> dict[str, Any]:
    """Delete exactly our Terms of *db* (marker ``wiki-e:<db>:``), then reindex."""
    api.database(db)
    ours = [t for t in api.terms() if our_marker(t.get("description"), db)]
    result: dict[str, Any] = {
        "action": "remove",
        "database": db,
        "removed": 0,
        "failures": [],
    }
    for term in ours:
        resp = api.delete_term(term["id"])
        if resp.status_code >= 400:
            result["failures"].append(f"{term['name']}: HTTP {resp.status_code}")
        else:
            result["removed"] += 1
    left = [t for t in api.terms() if our_marker(t.get("description"), db)]
    if left:
        result["failures"].append(f"{len(left)} wiki-e Term(s) still stored")
    result["reindex"] = lsb_kb._reindex(api, db)
    return result


def write_log(out_dir: Path, db: str, result: dict[str, Any]) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"wiki-load-{db}-{result['action']}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# wiki-score-extraction
# --------------------------------------------------------------------------- #

_NUM = re.compile(r"\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9]+")


def _name_keys(name: str) -> set[str]:
    """Normalised keys for a name: whole, without parentheses, the parenthesised part."""
    keys = {norm_name(name)}
    base = re.sub(r"\(.*?\)", " ", name)
    keys.add(norm_name(base))
    keys.update(norm_name(m) for m in re.findall(r"\((.*?)\)", name))
    return {k for k in keys if k}


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(wa & wb) / len(wa | wb) if wa | wb else 1.0


def score_extraction(
    doc: dict[str, Any], kb: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Recall of the official KB terms in an extraction, by normalised name.

    ``to_eyeball`` lists matched pairs, least similar first, with the numbers
    that appear on one side only: a different threshold or constant shows up
    there. It reads text only; whether a formula is *equivalent* is for a human.
    """
    extracted: dict[str, dict[str, Any]] = {}
    for entry in doc["terms"]:
        for key in _name_keys(str(entry.get("name") or "")) | _name_keys(
            str(entry.get("abbreviation") or "")
        ):
            extracted.setdefault(key, entry)
    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    for kb_id, kb_entry in kb.items():
        name = str(kb_entry.get("knowledge") or "").strip()
        hit = next((extracted[k] for k in _name_keys(name) if k in extracted), None)
        if hit is None:
            missing.append(name)
            continue
        kb_text = " ".join(
            str(kb_entry.get(f) or "") for f in ("description", "definition")
        )
        our_text = (
            " ".join(str(hit.get(f) or "") for f in ("definition", "formula"))
            + " "
            + " ".join(map(str, hit.get("thresholds") or []))
        )
        kb_nums, our_nums = set(_NUM.findall(kb_text)), set(_NUM.findall(our_text))
        matched.append(
            {
                "kb_id": kb_id,
                "kb_name": name,
                "extracted_name": hit.get("name"),
                "similarity": round(_jaccard(kb_text, our_text), 3),
                "numbers_only_in_kb": sorted(kb_nums - our_nums),
                "numbers_only_in_extraction": sorted(our_nums - kb_nums),
            }
        )
    matched.sort(key=lambda m: (m["similarity"], m["kb_id"]))
    total = len(kb)
    kb_keys = {
        k for e in kb.values() for k in _name_keys(str(e.get("knowledge") or ""))
    }
    extra = [
        str(e.get("name"))
        for e in doc["terms"]
        if not _name_keys(str(e.get("name") or "")) & kb_keys
    ]
    return {
        "kb_terms": total,
        "extracted_terms": len(doc["terms"]),
        "matched": len(matched),
        "recall": round(len(matched) / total, 3) if total else 0.0,
        "missing": sorted(missing),
        "extracted_not_in_kb": sorted(extra),
        "to_eyeball": matched,
    }
