# SPDX-License-Identifier: Apache-2.0
"""Model-assisted business-domain grouping for Exploration Terms.

Three pieces:

* :func:`build_prompt` and :func:`parse_grouping` are pure and unit-tested — the
  prompt shows each Term as id, name, a description cut to 120 characters and its
  synonyms, and demands strict JSON; the parser enforces every grouping rule
  (unknown ids dropped, first assignment of a duplicate kept, small domains and
  stragglers folded into "Other", at most 8 domains, names trimmed and
  de-duplicated) and raises :class:`GroupingError` on anything unparsable.
* :func:`group_terms` makes one model call with ONE retry on invalid output,
  then falls back to the deterministic :func:`dal.term_domains.cluster_terms`
  (origin stays ``auto``, names are the most connected Term) with a logged
  warning.
* Databases with more than 200 Terms are sent name-sorted batches of 200: the
  first batch defines the domains, and each later batch only ASSIGNS its Terms
  to those domains (or "Other"), deterministically at temperature 0.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage

from auto_ontology.dal.term_domains import cluster_terms
from auto_ontology.retrieval.text_to_sql.question_language import _content_text

logger = logging.getLogger(__name__)

MAX_BATCH = 200
# Generous on purpose: a reasoning model spends part of the budget thinking, and an
# exhausted budget comes back as an empty reply (seen on a 95-Term database at 4000).
MAX_TOKENS = 16000
MAX_DOMAINS = 8


class GroupingError(ValueError):
    """The model output could not be interpreted as a valid grouping."""


def build_prompt(terms: list[dict[str, Any]], language: str) -> str:
    """Build a JSON-only grouping prompt in the database ontology language."""
    compact = [
        {
            "id": str(term["id"]),
            "name": str(term.get("name") or ""),
            "description": str(term.get("description") or "")[:120],
            "synonyms": term.get("synonyms") or [],
        }
        for term in terms
    ]
    # Fewer than 3 domains is allowed only when the database has fewer than 12
    # Terms; above that the model must try for a real 3-to-8 business split.
    count_rule = (
        "3 to 8" if len(terms) >= 12 else "as few as needed, at least 1, up to 8"
    )
    return (
        "Group these ontology Terms into business domains a manager would use. "
        f"Write domain names and descriptions in language code {language}. Return "
        f'strict JSON with shape {{"domains":[{{"name":str,"description":str,'
        f'"term_ids":[str]}}]}}. Use {count_rule} domains. Assign every given '
        "Term id exactly once; do not invent ids. Every domain needs at least 2 "
        "Terms: put a Term that fits nowhere into the closest domain, never alone "
        "and never in a catch-all. Business names a manager would say, such as "
        "Finance or Marketing; never generic names like Group 1. Reply with the "
        "JSON only, no code fence and no commentary.\n"
        + json.dumps({"terms": compact}, ensure_ascii=False)
    )


def _json_text(raw: str) -> str:
    """The JSON object inside a model reply, tolerating code fences and prose."""
    text = str(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def parse_grouping(raw: str, valid_term_ids: set[str]) -> list[dict[str, Any]]:
    """Validate and normalise a model grouping; raise GroupingError if unparsable.

    Unknown ids are dropped, the first assignment of a duplicated id is kept,
    every unassigned Term lands in a domain named "Other" (created only when
    needed), domains with fewer than 2 Terms merge into "Other", the result is
    capped at 8 domains, and names are trimmed and de-duplicated.
    """
    try:
        data = json.loads(_json_text(raw))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Grouping output is not JSON; it starts with: %r", str(raw)[:200]
        )
        raise GroupingError("Grouping output is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("domains"), list):
        raise GroupingError("Grouping output must contain a domains list")
    domains: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for item in data["domains"]:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            continue
        ids: list[str] = []
        raw_ids = item.get("term_ids", [])
        if not isinstance(raw_ids, list):
            continue
        for raw_id in raw_ids:
            term_id = str(raw_id)
            if term_id in valid_term_ids and term_id not in assigned:
                ids.append(term_id)
                assigned.add(term_id)
        if ids:
            domains.append(
                {"name": name, "description": item.get("description"), "term_ids": ids}
            )
    missing = sorted(valid_term_ids - assigned)

    def other_domain() -> dict[str, Any]:
        for candidate in domains:
            if candidate["name"].casefold() == "other":
                return candidate
        created = {"name": "Other", "description": None, "term_ids": []}
        domains.append(created)
        return created

    # A one-Term domain carries no business meaning; fold it into Other.
    small = [domain for domain in domains if len(domain["term_ids"]) < 2]
    for domain in small:
        domains.remove(domain)
        other_domain()["term_ids"].extend(domain["term_ids"])
    if missing:
        other_domain()["term_ids"].extend(missing)
    domains = [domain for domain in domains if domain["term_ids"]]
    if not domains and valid_term_ids:
        domains = [
            {"name": "Other", "description": None, "term_ids": sorted(valid_term_ids)}
        ]
    # Merge duplicate names case-insensitively, keeping the first description.
    merged: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for domain in domains:
        target = by_name.get(domain["name"].casefold())
        if target is None:
            target = {**domain, "term_ids": list(domain["term_ids"])}
            by_name[target["name"].casefold()] = target
            merged.append(target)
        else:
            target["term_ids"].extend(domain["term_ids"])
    # Cap at 8: any extra domains pour their Terms into an existing "Other",
    # or into the last slot renamed "Other" when there is none.
    if len(merged) > MAX_DOMAINS:
        overflow: list[str] = []
        for domain in merged[MAX_DOMAINS:]:
            overflow.extend(domain["term_ids"])
        target = next(
            (d for d in merged[:MAX_DOMAINS] if d["name"].casefold() == "other"),
            None,
        )
        if target is None:
            target = merged[MAX_DOMAINS - 1]
            target["name"] = "Other"
            target["description"] = None
        target["term_ids"].extend(overflow)
        merged = merged[:MAX_DOMAINS]
    return merged


def _parse_assignment(
    raw: str, valid_term_ids: set[str], known_names: list[str]
) -> list[dict[str, Any]]:
    """Parse an assignment batch: every Term mapped to one known domain name.

    Unlike :func:`parse_grouping` this applies no minimum size — a batch may
    legitimately send a single Term to a domain — and an unknown or missing name
    falls back to "Other".
    """
    try:
        data = json.loads(_json_text(raw))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Grouping output is not JSON; it starts with: %r", str(raw)[:200]
        )
        raise GroupingError("Assignment output is not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("domains"), list):
        raise GroupingError("Assignment output must contain a domains list")
    known = {name.casefold(): name for name in known_names}
    assigned: dict[str, str] = {}
    for item in data["domains"]:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            continue
        target = known.get(name.casefold(), "Other")
        raw_ids = item.get("term_ids", [])
        if not isinstance(raw_ids, list):
            continue
        for raw_id in raw_ids:
            term_id = str(raw_id)
            if term_id in valid_term_ids and term_id not in assigned:
                assigned[term_id] = target
    groups: dict[str, list[str]] = {}
    for term_id in sorted(valid_term_ids):
        # A Term the batch never named goes to Other, never disappears.
        groups.setdefault(assigned.get(term_id, "Other"), []).append(term_id)
    return [
        {"name": name, "description": None, "term_ids": ids}
        for name, ids in sorted(groups.items())
    ]


def _invoke(llm: Any, prompt: str) -> str:
    bound = llm.bind(max_tokens=MAX_TOKENS, temperature=0)
    reply = bound.invoke([HumanMessage(content=prompt)])
    return _content_text(getattr(reply, "content", ""))


def _fallback(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic graph clustering; names stay the most connected Term."""
    result = cluster_terms(terms, [])
    return [
        {"name": domain["name"], "description": None, "term_ids": domain["term_ids"]}
        for domain in result["domains"]
    ]


def group_terms(
    terms: list[dict[str, Any]], language: str, llm: Any
) -> list[dict[str, Any]]:
    """Group Terms with one retry, then the deterministic clustering fallback."""
    ordered = sorted(
        terms,
        key=lambda term: (str(term.get("name") or "").casefold(), str(term["id"])),
    )
    if not ordered:
        return []
    try:
        batch = ordered[:MAX_BATCH]
        prompt = build_prompt(batch, language)
        parsed: list[dict[str, Any]] | None = None
        for attempt in range(2):
            try:
                parsed = parse_grouping(
                    _invoke(llm, prompt), {str(term["id"]) for term in batch}
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry malformed output once
                if attempt == 1:
                    raise
                logger.warning("Domain grouping attempt failed; retrying: %s", exc)
        assert parsed is not None
        known_names = [domain["name"] for domain in parsed]
        # Later name-sorted batches only ASSIGN their Terms to the domains the
        # first batch defined, so the split stays stable however big the database.
        for offset in range(MAX_BATCH, len(ordered), MAX_BATCH):
            remaining = ordered[offset : offset + MAX_BATCH]
            try:
                domain_slots = [{"name": name, "term_ids": []} for name in known_names]
                other_slot = [{"name": "Other", "term_ids": []}]
                assignment_prompt = (
                    "Assign each Term to exactly one of these existing domain names, or "
                    "Other. Write nothing but strict JSON with shape "
                    '{"domains":[{"name":str,"term_ids":[str]}]}; do not invent '
                    "ids or new domains.\n"
                    + json.dumps(
                        {
                            "domains": domain_slots + other_slot,
                            "terms": [
                                {
                                    "id": str(term["id"]),
                                    "name": str(term.get("name") or ""),
                                }
                                for term in remaining
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
                assigned = _parse_assignment(
                    _invoke(llm, assignment_prompt),
                    {str(term["id"]) for term in remaining},
                    known_names + ["Other"],
                )
            except Exception:  # noqa: BLE001 - an unusable batch lands in Other
                logger.warning(
                    "Domain assignment batch failed; using Other", exc_info=True
                )
                assigned = [
                    {
                        "name": "Other",
                        "description": None,
                        "term_ids": [str(term["id"]) for term in remaining],
                    }
                ]
            for group in assigned:
                target = next(
                    (
                        domain
                        for domain in parsed
                        if domain["name"].casefold() == group["name"].casefold()
                    ),
                    None,
                )
                if target is None:
                    target = {
                        "name": group["name"],
                        "description": None,
                        "term_ids": [],
                    }
                    parsed.append(target)
                target["term_ids"].extend(group["term_ids"])
        return parsed
    except Exception:
        logger.warning(
            "Business-domain grouping fallback was used: deterministic clustering "
            "replaced the model grouping (output unusable twice)",
            exc_info=True,
        )
        return _fallback(ordered)
