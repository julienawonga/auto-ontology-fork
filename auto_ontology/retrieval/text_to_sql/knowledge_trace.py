# SPDX-License-Identifier: Apache-2.0

"""Knowledge-use trace: what business knowledge SQL generation saw, and what the
final SQL appears to have used.

Every answer carries ``knowledge_trace``, a list of entries::

    {"kind": "term" | "sql_attribute" | "column_attribute" | "custom_analysis",
     "id": ..., "name": ..., "database": ..., "certified": True | False | None,
     "reason": "why it was offered", "used": True | False | None,
     "matched": ["identifiers/literals of the entry found in the SQL"]}

*Offered* means the entry was part of what the SQL generator was given (see
``_offered_*``). *Used* is a deterministic text heuristic on the final SQL, not
a proof: it asks whether the entry's own identifiers or literals appear in the
query. ``None`` means the heuristic cannot decide. Nothing here calls a model,
changes a prompt or touches ``evidence``: the trace is read off the state after
generation. The provenance this gives (which definition shaped which answer) is
the input a later review step needs; it is not itself a quality signal.

Heuristic, per kind (tokens = identifiers and quoted literals, lower-cased,
SQL keywords and common functions ignored):

* ``sql_attribute`` / ``custom_analysis``: the snippet (expression, else SQL)
  appears whitespace- and case-insensitively inside the SQL -> used. Else if
  every distinctive token of the snippet is in the SQL -> used; none -> not
  used; some -> undecidable.
* ``term``: candidates taken from its definition: backticked names, quoted
  literals, ``snake_case`` and ``table.column`` identifiers, plus bare words
  written as identifiers in prose or math (``TechSigProb``, ``BWHz``: a word
  with a capital after its first letter) that are real column names of the
  tables the generator was shown. Any in the SQL -> used (``matched`` lists
  them); candidates but none present -> not used; no candidates (a purely
  prose definition, or one naming only all-lowercase words) -> undecidable. A
  term is also used when one of its own SQL attributes is.
* ``column_attribute``: the physical column it is attached to (when known)
  appears as an identifier in the SQL -> used, absent -> not used, column
  unknown -> its display name is tried and otherwise undecidable. A column
  reached only through ``SELECT *`` counts as absent.
"""

from __future__ import annotations

import logging
import re

from typing import Any, Callable

logger = logging.getLogger(__name__)

KINDS = ("term", "sql_attribute", "column_attribute", "custom_analysis")
_MAX_MATCHED = 5

_STOP = frozenset(
    """select from where and or not as on in is null by group order having limit
    offset join left right inner outer full cross union all distinct case when
    then else end between like ilike exists asc desc with over partition cast
    sum count avg min max coalesce nullif round extract date true false using
    interval filter within current_date now text integer int numeric float
    double precision varchar boolean""".split()
)

_IDENT = re.compile(r'"?([A-Za-z_][A-Za-z0-9_$]*)"?')
_STRING = re.compile(r"'((?:[^']|'')*)'")
_BACKTICK = re.compile(r"`([^`]+)`")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

CertifiedLookup = Callable[[str], bool | None]


def _norm(text: str) -> str:
    return re.sub(r"[\s\"]+", "", text.lower())


def _tokens(text: str) -> set[str]:
    """Identifiers and string literals of *text*, minus SQL noise."""
    literals = {m.lower() for m in _STRING.findall(text) if m.strip()}
    bare = _STRING.sub(" ", text)
    idents = {m.lower() for m in _IDENT.findall(bare)}
    return {t for t in idents if t not in _STOP and len(t) >= 3} | literals


def _snippet_state(snippet: str, sql: str) -> tuple[bool | None, list[str]]:
    snippet = (snippet or "").strip().rstrip(";")
    if not snippet:
        return None, []
    sql_tokens = _tokens(sql)
    wanted = _tokens(snippet)
    hit = sorted(wanted & sql_tokens)[:_MAX_MATCHED]
    if len(_norm(snippet)) >= 4 and _norm(snippet) in _norm(sql):
        return True, hit
    if not wanted:
        return None, []
    if wanted <= sql_tokens:
        return True, hit
    if not hit:
        return False, []
    return None, hit


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _schema_words(description: str, columns: set[str]) -> set[str]:
    """Words of a definition written as identifiers that are real columns.

    Knowledge-base definitions put column names in prose or math without
    quoting (``TOLS = TechSigProb * (1 - NatSrcProb)``). Only a word with a
    capital after its first letter counts, so an ordinary word that happens to
    be a column name (``status``) does not make a term look used.
    """
    out: set[str] = set()
    for word in _WORD.findall(description):
        low = word.lower()
        if low in columns and low not in _STOP and word[1:] != word[1:].lower():
            out.add(low)
    return out


def _term_candidates(description: str, columns: set[str] | None = None) -> set[str]:
    """Distinctive strings a definition names, for the term heuristic."""
    cands: set[str] = _schema_words(description, columns or set())
    for m in _BACKTICK.findall(description):
        cands |= _tokens(m) | {m.strip().lower()}
    for m in _STRING.findall(description):
        if m.strip():
            cands.add(m.lower())
    for m in _SNAKE.findall(description):
        cands.add(m.lower())
    for a, b in _DOTTED.findall(description):
        cands |= {t for t in (a.lower(), b.lower()) if t not in _STOP and len(t) >= 3}
    return {c for c in cands if c and c not in _STOP}


def _term_state(
    description: str, sql: str, columns: set[str] | None = None
) -> tuple[bool | None, list[str]]:
    cands = _term_candidates(description, columns)
    if not cands:
        return None, []
    sql_tokens = _tokens(sql)
    sql_norm = _norm(sql)
    hit = sorted(c for c in cands if c in sql_tokens or (len(c) >= 4 and c in sql_norm))
    if hit:
        return True, hit[:_MAX_MATCHED]
    return False, []


def _default_certified(attr_id: str) -> bool | None:
    """Certification of a SqlAttribute from the store; None if unavailable."""
    try:
        from auto_ontology.dal.sql_attributes import get_full_sql_attribute_by_id

        row = get_full_sql_attribute_by_id(attr_id)
    except Exception:
        logger.debug("certification lookup failed for %s", attr_id, exc_info=True)
        return None
    if not row or row.get("certified") is None:
        return None
    return bool(row["certified"])


def _hit_database(hits: list[dict], hit_id: str) -> str | None:
    for hit in hits:
        if str(hit.get("id")) == hit_id:
            return hit.get("database_name")
    return None


def build_knowledge_trace(
    path_state: dict[str, Any],
    final_sql: str | None,
    *,
    certified_lookup: CertifiedLookup | None = None,
) -> list[dict[str, Any]]:
    """The trace for one answer; see the module docstring for the contract.

    *path_state* is the agent state after the run. With no final SQL every
    ``used`` is ``None``. Never raises on odd state: malformed entries are
    skipped.
    """
    sql = (final_sql or "").strip()
    lookup = certified_lookup or _default_certified
    db = path_state.get("target_db")
    entries: list[dict[str, Any]] = []

    def used_for(state_fn: Callable[[], tuple[bool | None, list[str]]]):
        if not sql:
            return None, []
        return state_fn()

    columns = {
        str(c.get("name") or "").lower()
        for t in path_state.get("relevant_tables") or []
        if isinstance(t, dict)
        for c in t.get("columns") or []
        if isinstance(c, dict) and c.get("name")
    }
    contexts = path_state.get("column_attribute_contexts") or {}
    sa_used_terms: set[str] = set()
    retrieved_sa = list(path_state.get("retrieved_sql_attributes") or [])
    for a in path_state.get("sql_attributes") or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        aid = str(a["id"])
        used, matched = used_for(
            lambda a=a: _snippet_state(a.get("expression") or a.get("sql") or "", sql)
        )
        if used and a.get("term_name"):
            sa_used_terms.add(str(a["term_name"]).strip().lower())
        term = (a.get("term_name") or "").strip()
        entries.append(
            {
                "kind": "sql_attribute",
                "id": aid,
                "name": a.get("name") or "",
                "database": _hit_database(retrieved_sa, aid) or db,
                "certified": lookup(aid),
                "reason": "retrieved for the question"
                + (f"; defines Term {term!r}" if term else ""),
                "used": used,
                "matched": matched,
            }
        )

    retrieved_ca = list(path_state.get("retrieved_custom_analyses") or [])
    for c in path_state.get("custom_analyses") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        used, matched = used_for(lambda c=c: _snippet_state(c.get("sql") or "", sql))
        entries.append(
            {
                "kind": "custom_analysis",
                "id": str(c["id"]),
                "name": c.get("name") or "",
                "database": _hit_database(retrieved_ca, str(c["id"])) or db,
                "certified": None,
                "reason": "retrieved for the question",
                "used": used,
                "matched": matched,
            }
        )

    for h in path_state.get("retrieved_column_attributes") or []:
        if not isinstance(h, dict) or not h.get("id"):
            continue
        name = str(h.get("name") or "")
        used: bool | None = None
        matched: list[str] = []
        ctx = contexts.get(str(h["id"])) or {}
        col = str(ctx.get("col_name") or "").strip().lower()
        key = col or re.sub(r"\W+", "_", name.strip().lower()).strip("_")
        if sql and len(key) >= 3:
            if key in _tokens(sql):
                used, matched = True, [key]
            elif col:
                used = False
        entries.append(
            {
                "kind": "column_attribute",
                "id": str(h["id"]),
                "name": name,
                "database": h.get("database_name") or db,
                "certified": None,
                "reason": "retrieved for the question; chooses tables and columns shown"
                + (f"; column {col!r}" if col else ""),
                "used": used,
                "matched": matched,
            }
        )

    for t in path_state.get("offered_terms") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        used, matched = used_for(
            lambda t=t: _term_state(t.get("description") or "", sql, columns)
        )
        reason = (
            "retrieved for the question"
            if not t.get("depth")
            else f"named by a retrieved definition (depth {t['depth']})"
        )
        if sql and str(t.get("name") or "").strip().lower() in sa_used_terms:
            used = True
            matched = (matched + ["its sql_attribute is used"])[:_MAX_MATCHED]
        entries.append(
            {
                "kind": "term",
                "id": str(t["id"]),
                "name": t.get("name") or "",
                "database": db,
                "certified": t.get("certified"),
                "reason": reason,
                "used": used,
                "matched": matched,
            }
        )
    return entries


def summarize(trace: list[dict[str, Any]]) -> str:
    """One log line: offered/used counts overall and per kind."""
    parts = []
    for kind in KINDS:
        rows = [e for e in trace if e["kind"] == kind]
        if rows:
            parts.append(f"{kind}={len(rows)}/{sum(1 for e in rows if e['used'])}")
    certified_used = sum(1 for e in trace if e["used"] and e.get("certified"))
    return (
        f"Knowledge trace: offered {len(trace)}, used {sum(1 for e in trace if e['used'])}"
        f", undecided {sum(1 for e in trace if e['used'] is None)}"
        f", certified-and-used {certified_used} ({' '.join(parts) or 'none'};"
        " per kind = offered/used)"
    )
