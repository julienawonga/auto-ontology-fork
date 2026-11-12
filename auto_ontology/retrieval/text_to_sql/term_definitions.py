# SPDX-License-Identifier: Apache-2.0

"""Give the business definitions of the relevant Terms to SQL generation.

A Term's description is where a team writes down what a business word means
("Net Revenue is gross sales minus refunds"). Retrieval used to keep one
"subject" Term and use it only to add that Term's tables, so the definition
itself never reached the SQL generator. This module retrieves the Terms
relevant to the question, follows the definitions that name other Terms of the
same database, and renders them as a block appended to the request ``evidence``,
which SQL generation already treats as authoritative.

It is on by default; set ``TERM_DEFINITIONS_IN_GENERATION=false`` to turn it
off. The module changes nothing else: with the flag off the evidence is
returned as is. Measured on LiveSQLBench-Base-Lite (docs/evaluation.md): with
the knowledge loaded as Terms, 55/180 with it against 4/180 without.

Rendering keeps each description verbatim. Nothing is stripped: a generic rule
for dropping bracketed ``[xxx:...]`` lines would also eat legitimate text, and
a marker line costs a few characters out of the budget.
"""

from __future__ import annotations

import logging
import os
import re

from dataclasses import dataclass
from typing import Any, Callable

from auto_ontology.semantic.constants import LABEL_TERM

logger = logging.getLogger(__name__)

BLOCK_HEADER = "Business definitions"
_MIN_MENTION_LEN = 3

SearchFn = Callable[..., list[dict]]
LoadTermsFn = Callable[[str], list[dict[str, Any]]]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return default


@dataclass(frozen=True)
class TermDefinitionSettings:
    """Knobs, read from the environment at call time."""

    enabled: bool = True
    top_k: int = 5
    # Largest vector distance (lower is closer) a retrieved Term may have.
    # None keeps every hit.
    max_distance: float | None = None
    max_depth: int = 2
    max_terms: int = 12
    budget_chars: int = 6000

    @classmethod
    def from_env(cls) -> "TermDefinitionSettings":
        raw_distance = os.environ.get("TERM_DEFINITIONS_MAX_DISTANCE", "").strip()
        distance: float | None = None
        if raw_distance:
            try:
                distance = float(raw_distance)
            except ValueError:
                logger.warning(
                    "Ignoring non-numeric TERM_DEFINITIONS_MAX_DISTANCE=%r",
                    raw_distance,
                )
        return cls(
            enabled=_env_bool("TERM_DEFINITIONS_IN_GENERATION", True),
            top_k=max(1, _env_int("TERM_DEFINITIONS_TOP_K", 5)),
            max_distance=distance,
            max_depth=max(0, _env_int("TERM_DEFINITIONS_MAX_DEPTH", 2)),
            max_terms=max(1, _env_int("TERM_DEFINITIONS_MAX_TERMS", 12)),
            budget_chars=max(0, _env_int("TERM_DEFINITIONS_BUDGET_CHARS", 6000)),
        )


@dataclass(frozen=True)
class DefinedTerm:
    """One Term chosen for injection."""

    id: str
    name: str
    description: str
    certified: bool
    depth: int  # 0 = retrieved for the question, n = reached through n mentions


@dataclass(frozen=True)
class TermDefinitions:
    """What was selected and what will be injected."""

    block: str
    terms: list[DefinedTerm]
    rendered: list[DefinedTerm]

    @property
    def chars(self) -> int:
        return len(self.block)


_ABBREVIATION = re.compile(r"\(([^()]{%d,})\)\s*$" % _MIN_MENTION_LEN)


def _mention_names(name: str) -> list[str]:
    """Strings that count as a mention of a Term: its name, and a trailing
    parenthesised abbreviation (``Signal Quality (SNQI)`` -> ``SNQI``)."""
    names = [name.strip()]
    match = _ABBREVIATION.search(name)
    if match:
        names.append(match.group(1).strip())
    return [n for n in names if len(n) >= _MIN_MENTION_LEN]


def _mention_pattern(names: list[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    alternatives = "|".join(
        re.escape(n) for n in sorted(set(names), key=len, reverse=True)
    )
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def expand_by_mentions(
    seeds: list[DefinedTerm],
    catalogue: list[dict[str, Any]],
    *,
    max_depth: int,
    max_terms: int,
) -> list[DefinedTerm]:
    """Add Terms of *catalogue* whose name a selected definition mentions.

    Breadth first, so closer Terms come first; bounded by *max_depth* and
    *max_terms*. Case-insensitive, whole-word match on the name or on its
    trailing parenthesised abbreviation. A Term never mentions itself.
    """
    selected: list[DefinedTerm] = list(seeds)[:max_terms]
    known = {t.id for t in selected}
    patterns = {
        str(row["id"]): _mention_pattern(_mention_names(str(row.get("name") or "")))
        for row in catalogue
        if row.get("id") is not None
    }
    frontier = list(selected)
    depth = 0
    while frontier and depth < max_depth and len(selected) < max_terms:
        depth += 1
        next_frontier: list[DefinedTerm] = []
        for parent in frontier:
            for row in catalogue:
                rid = str(row.get("id") or "")
                if not rid or rid in known or len(selected) >= max_terms:
                    continue
                pattern = patterns.get(rid)
                if pattern is None or not pattern.search(parent.description):
                    continue
                child = DefinedTerm(
                    id=rid,
                    name=str(row.get("name") or ""),
                    description=str(row.get("description") or "").strip(),
                    certified=bool(row.get("certified")),
                    depth=depth,
                )
                known.add(rid)
                selected.append(child)
                next_frontier.append(child)
        frontier = next_frontier
    return selected


def render_block(
    terms: list[DefinedTerm], budget_chars: int
) -> tuple[str, list[DefinedTerm]]:
    """Render ``name: description`` lines within *budget_chars*.

    Certified Terms come first, then the order given (retrieval rank, then
    expansion order). A Term that does not fit whole is skipped, and a later,
    shorter one may still fit; a definition is never cut mid-sentence.
    Returns the block (empty when nothing fits) and the Terms it contains.
    """
    ordered = sorted(terms, key=lambda t: not t.certified)  # stable
    header = f"{BLOCK_HEADER}:\n"
    used = len(header)
    lines: list[str] = []
    kept: list[DefinedTerm] = []
    for term in ordered:
        if not term.description:
            continue
        line = f"- {term.name}: {term.description}\n"
        if used + len(line) > budget_chars:
            continue
        lines.append(line)
        kept.append(term)
        used += len(line)
    if not kept:
        return "", []
    return (header + "".join(lines)).rstrip("\n"), kept


def merge_evidence(evidence: str, block: str) -> str:
    """Request evidence first, the definitions after it."""
    evidence = (evidence or "").strip()
    if not block:
        return evidence
    if not evidence:
        return block
    return f"{evidence}\n\n{block}"


def _default_search(*args: Any, **kwargs: Any) -> list[dict]:
    from auto_ontology.retrieval.data_access.semantic_search import (
        search_semantic_index,
    )

    return search_semantic_index(*args, **kwargs)


def _default_load_terms(database_name: str) -> list[dict[str, Any]]:
    from auto_ontology.dal.terms import fetch_database_term_definitions

    return fetch_database_term_definitions(database_name)


def build_term_definitions(
    retriever: object,
    question: str,
    database_name: str | None,
    settings: TermDefinitionSettings,
    *,
    search: SearchFn | None = None,
    load_terms: LoadTermsFn | None = None,
) -> TermDefinitions:
    """Retrieve, expand and render the definitions for one question."""
    empty = TermDefinitions("", [], [])
    if not (database_name and question.strip()):
        return empty
    search = search or _default_search
    load_terms = load_terms or _default_load_terms

    hits = search(
        retriever,
        question,
        label_filter=[LABEL_TERM],
        per_label_k=settings.top_k,
        database_name=database_name,
    )
    ranked: list[str] = []
    for hit in sorted(hits, key=lambda h: float(h.get("score") or 0.0)):
        score = hit.get("score")
        distance = float(score) if score is not None else float("inf")
        if settings.max_distance is not None and distance > settings.max_distance:
            continue
        hit_id = str(hit.get("id") or "")
        if hit_id and hit_id not in ranked:
            ranked.append(hit_id)
    if not ranked:
        return empty

    catalogue = load_terms(database_name)
    by_id = {str(r["id"]): r for r in catalogue if r.get("id") is not None}
    seeds = [
        DefinedTerm(
            id=tid,
            name=str(by_id[tid].get("name") or ""),
            description=str(by_id[tid].get("description") or "").strip(),
            certified=bool(by_id[tid].get("certified")),
            depth=0,
        )
        for tid in ranked
        if tid in by_id
    ]
    terms = expand_by_mentions(
        seeds,
        catalogue,
        max_depth=settings.max_depth,
        max_terms=settings.max_terms,
    )
    block, rendered = render_block(terms, settings.budget_chars)
    return TermDefinitions(block, terms, rendered)
