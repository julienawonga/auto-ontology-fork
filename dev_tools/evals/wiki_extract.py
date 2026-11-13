# SPDX-License-Identifier: Apache-2.0

"""Experiment E, arm E-auto: compile wiki chunks into reviewable ontology Terms.

Each chunk goes to an LLM with a strict JSON schema. A term is kept only when
its supporting ``quote`` (and its ``formula``, when given) is a verbatim
substring of the chunk it came from; anything else is rejected and logged, so
a paraphrase or an invented formula never reaches the ontology. The kept terms
are consolidated (deduplicated by normalised name, merged, contradictions and
dependencies on undefined terms flagged) into a YAML file a human reviews
(step E-relu) before ``wiki-load`` stores it.

The LLM is an injected callable ``(system, user) -> text``; the default one
talks to the repo's configured OpenAI-compatible gateway (same ``REASONING_*``
/ ``DEFAULT_MODELS_*`` settings as the backend). Tests use fakes.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from dev_tools.evals.wiki import Chunk, slug

logger = logging.getLogger(__name__)

LlmFn = Callable[[str, str], str]

SYSTEM_PROMPT = """You extract business terms from one section of a corporate \
knowledge-base wiki. Reply with ONE JSON object and nothing else:

{"terms": [{
  "name": "term name as written in the text",
  "abbreviation": "abbreviation, or null",
  "definition": "what the term means, using the text's own wording",
  "formula": "the formula copied CHARACTER FOR CHARACTER from the text, or null",
  "thresholds": ["each threshold, class or category rule, copied from the text"],
  "depends_on": ["names of other terms this one is computed from or uses"],
  "candidate_columns": ["database column names the text mentions for this term"],
  "quote": "an EXACT, contiguous quote from the text that supports the term"
}]}

Rules: extract only terms the text defines or computes. Never invent a term, \
a number or a formula. `quote` and `formula` must be copied verbatim (keep any \
LaTeX exactly as written, with its backslashes). Use [] when a list is empty \
and null when a field is absent. If the section defines no term, reply \
{"terms": []}."""

_STR_FIELDS = ("name", "definition", "quote")
_OPT_STR = ("abbreviation", "formula")
_LIST_FIELDS = ("thresholds", "depends_on", "candidate_columns")
# JSON decodes an unescaped LaTeX "\text" as TAB+"ext"; map the common victims back.
_MANGLED = {"\t": "\\t", "\f": "\\f", "\b": "\\b", "\r": "\\r"}


class ExtractionError(Exception):
    """The model's reply could not be used at all."""


def norm_name(name: str) -> str:
    """Normalised term name: lowercase, alphanumerics and single spaces."""
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def _unmangle(text: str) -> str:
    for bad, good in _MANGLED.items():
        text = text.replace(bad, good)
    return text


def parse_reply(reply: str) -> list[dict[str, Any]]:
    """The raw term dicts of a reply (code fences and chatter tolerated)."""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ExtractionError("no JSON object in the reply")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid JSON: {exc}") from exc
    terms = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms, list):
        raise ExtractionError("reply has no 'terms' list")
    return [t for t in terms if isinstance(t, dict)]


_UNICODE_DASH_QUOTE_MAP = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def _normalize_for_match(text: str) -> str:
    """Normalize text for matching formulas and quotes against wiki chunks.

    Rules in order:
    1. Removes $$ and single $ math delimiters.
    2. Replaces \\text{...}, \\mathrm{...}, \\textbf{...}, \\operatorname{...} by their content (non-nested).
    3. Removes sizing/spacing commands: \\bigl, \\bigr, \\Bigl, \\Bigr, \\left, \\right, \\,, \\;, \\:, \\!, \\quad, \\qquad.
    4. Maps \\times and \\cdot to *, \\frac{a}{b} to (a)/(b) (non-nested), \\% to %, \\_ to _.
    5. Maps unicode dashes –, —, − to -, curly quotes to straight quotes, and non-breaking spaces to spaces.
    6. Drops every remaining whitespace character entirely and casefolds.
    """
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(
        r"\\(?:text|mathrm|textbf|operatorname)\{([^}]*)\}",
        r"\1",
        text,
    )
    text = re.sub(
        r"\\(?:qquad|quad|bigl|bigr|Bigl|Bigr|left|right)(?![a-zA-Z])|\\[,;:!]",
        "",
        text,
    )
    text = text.replace(r"\times", "*").replace(r"\cdot", "*")
    text = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"(\1)/(\2)", text)
    text = text.replace(r"\%", "%").replace(r"\_", "_")
    text = text.translate(_UNICODE_DASH_QUOTE_MAP)
    return re.sub(r"\s+", "", text).casefold()


def _verbatim(value: str, chunk_text: str) -> str | None:
    """*value* when it occurs verbatim in the chunk, else a repaired copy that
    does (LaTeX backslashes eaten by JSON), else a normalized match, else None."""
    candidates = (value, _unmangle(value), _unmangle(value).replace("\n", "\\n"))
    for cand in candidates:
        if cand and cand in chunk_text:
            return cand

    norm_chunk = _normalize_for_match(chunk_text)
    for cand in candidates:
        if cand:
            norm_cand = _normalize_for_match(cand)
            if len(norm_cand) >= 8 and norm_cand in norm_chunk:
                return value

    return None


@dataclass
class Reject:
    chunk_id: str
    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"chunk_id": self.chunk_id, "name": self.name, "reason": self.reason}


@dataclass
class RawTerm:
    name: str
    abbreviation: str
    definition: str
    formula: str
    thresholds: list[str]
    depends_on: list[str]
    candidate_columns: list[str]
    chunk_id: str
    heading_path: list[str]
    quote: str


def validate_terms(
    chunk: Chunk, raw: Sequence[dict[str, Any]]
) -> tuple[list[RawTerm], list[Reject]]:
    """Schema check plus the verbatim-quote rule for one chunk's terms."""
    good: list[RawTerm] = []
    bad: list[Reject] = []
    for item in raw:
        name = str(item.get("name") or "").strip()
        label = name or "?"
        if any(
            not isinstance(item.get(f), str) or not item[f].strip() for f in _STR_FIELDS
        ):
            bad.append(
                Reject(chunk.id, label, "missing or empty name/definition/quote")
            )
            continue
        lists = {f: item.get(f) or [] for f in _LIST_FIELDS}
        if any(
            not isinstance(v, list) or any(not isinstance(x, str) for x in v)
            for v in lists.values()
        ):
            bad.append(
                Reject(
                    chunk.id,
                    label,
                    "thresholds/depends_on/candidate_columns must be string lists",
                )
            )
            continue
        if any(
            item.get(f) is not None and not isinstance(item.get(f), str)
            for f in _OPT_STR
        ):
            bad.append(
                Reject(chunk.id, label, "abbreviation/formula must be a string or null")
            )
            continue
        quote = _verbatim(item["quote"].strip(), chunk.text)
        if quote is None:
            bad.append(
                Reject(
                    chunk.id, label, "quote is not a verbatim substring of the chunk"
                )
            )
            continue
        formula = (item.get("formula") or "").strip()
        if formula:
            checked = _verbatim(formula, chunk.text)
            if checked is None:
                bad.append(
                    Reject(
                        chunk.id,
                        label,
                        "formula is not a verbatim substring of the chunk",
                    )
                )
                continue
            formula = checked
        good.append(
            RawTerm(
                name=name,
                abbreviation=(item.get("abbreviation") or "").strip(),
                definition=_unmangle(item["definition"].strip()),
                formula=formula,
                thresholds=[
                    _unmangle(x.strip()) for x in lists["thresholds"] if x.strip()
                ],
                depends_on=[x.strip() for x in lists["depends_on"] if x.strip()],
                candidate_columns=[
                    x.strip() for x in lists["candidate_columns"] if x.strip()
                ],
                chunk_id=chunk.id,
                heading_path=list(chunk.path),
                quote=quote,
            )
        )
    return good, bad


def extract_chunk(
    chunk: Chunk, llm: LlmFn, *, attempts: int = 2
) -> tuple[list[RawTerm], list[Reject]]:
    """One chunk through the model (one retry on an unusable reply)."""
    user = f"Section: {chunk.heading or '(preamble)'}\n\nText:\n{chunk.text}"
    last = ""
    for _ in range(max(1, attempts)):
        try:
            return validate_terms(chunk, parse_reply(llm(SYSTEM_PROMPT, user)))
        except ExtractionError as exc:
            last = str(exc)
    logger.warning("chunk %s: no usable reply (%s)", chunk.id, last)
    return [], [Reject(chunk.id, "*", f"chunk failed: {last}")]


def extract_all(
    chunks: Sequence[Chunk], llm: LlmFn, *, workers: int = 1
) -> tuple[list[RawTerm], list[Reject]]:
    """Every chunk, results in chunk order whatever the worker count."""
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda c: extract_chunk(c, llm), chunks))
    else:
        results = [extract_chunk(c, llm) for c in chunks]
    terms = [t for good, _ in results for t in good]
    rejects = [r for _, bad in results for r in bad]
    for r in rejects:
        logger.warning("rejected %s in %s: %s", r.name, r.chunk_id, r.reason)
    return terms, rejects


# --------------------------------------------------------------------------- #
# Consolidation
# --------------------------------------------------------------------------- #


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def consolidate(raw: Sequence[RawTerm]) -> list[dict[str, Any]]:
    """Merge the per-chunk terms into one entry per normalised name.

    A name that equals another term's abbreviation is the same term. Entries
    keep every source (chunk id, heading path, quote). Flags: ``contradiction``
    (same term, different formula or thresholds across sources; the first is
    primary and the others are kept under ``variants``) and
    ``undefined_dependency`` (depends on a name no term defines).
    """
    by_key: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    order: list[str] = []
    for t in raw:
        key = alias.get(norm_name(t.name)) or norm_name(t.name)
        if not key:
            continue
        if key not in by_key:
            order.append(key)
            by_key[key] = {
                "name": t.name,
                "abbreviation": t.abbreviation,
                "definition": t.definition,
                "formula": t.formula,
                "thresholds": list(t.thresholds),
                "depends_on": list(t.depends_on),
                "candidate_columns": list(t.candidate_columns),
                "sources": [],
                "variants": [],
                "flags": [],
            }
        entry = by_key[key]
        if t.abbreviation:
            alias.setdefault(norm_name(t.abbreviation), key)
            entry["abbreviation"] = entry["abbreviation"] or t.abbreviation
        entry["sources"].append(
            {"chunk_id": t.chunk_id, "heading_path": t.heading_path, "quote": t.quote}
        )
        if len(entry["sources"]) == 1:
            continue
        differs_formula = bool(
            t.formula
            and entry["formula"]
            and _squash(t.formula) != _squash(entry["formula"])
        )
        differs_thr = bool(
            t.thresholds
            and entry["thresholds"]
            and {_squash(x) for x in t.thresholds}
            != {_squash(x) for x in entry["thresholds"]}
        )
        if differs_formula or differs_thr:
            entry["variants"].append(
                {
                    "chunk_id": t.chunk_id,
                    "formula": t.formula,
                    "thresholds": t.thresholds,
                    "definition": t.definition,
                }
            )
            if "contradiction" not in entry["flags"]:
                entry["flags"].append("contradiction")
            continue
        # Same term seen again: fill gaps, keep the longer definition.
        if len(t.definition) > len(entry["definition"]):
            entry["definition"] = t.definition
        entry["formula"] = entry["formula"] or t.formula
        for f, vals in (
            ("thresholds", t.thresholds),
            ("depends_on", t.depends_on),
            ("candidate_columns", t.candidate_columns),
        ):
            have = {_squash(x) for x in entry[f]}
            entry[f] += [x for x in vals if _squash(x) not in have]

    defined = set(by_key) | set(alias)
    out: list[dict[str, Any]] = []
    for key in order:
        entry = by_key[key]
        missing = [d for d in entry["depends_on"] if norm_name(d) not in defined]
        if missing:
            entry["flags"].append("undefined_dependency")
            entry["undefined_dependencies"] = missing
        entry["id"] = slug(key)
        entry["include"] = True  # the reviewer sets false to drop a term
        entry["review_note"] = ""
        if not entry["variants"]:
            del entry["variants"]
        out.append(entry)
    return out


def build_document(
    db: str,
    wiki_sha256: str,
    terms: list[dict[str, Any]],
    rejects: Sequence[Reject],
    *,
    model: str = "",
    chunks: int = 0,
) -> dict[str, Any]:
    """The reviewable file: header, terms, and the rejected candidates."""
    return {
        "kind": "wiki-extraction",
        "database": db,
        "wiki_sha256": wiki_sha256,
        "model": model,
        "chunks": chunks,
        "terms": terms,
        "rejected": [r.to_dict() for r in rejects],
    }


# --------------------------------------------------------------------------- #
# Default LLM (the repo's configured gateway)
# --------------------------------------------------------------------------- #


def gateway_llm(
    role: str = "REASONING", *, timeout: float = 180.0
) -> tuple[LlmFn, str]:
    """(llm callable, model name) from the repo's ``<ROLE>_*`` model settings."""
    import httpx

    from auto_ontology.utils.model_config import require, resolve

    endpoint = require(role, "ENDPOINT").rstrip("/")
    model = require(role, "MODEL")
    key = resolve(role, "API_KEY")  # may be empty for a keyless endpoint
    url = (
        endpoint
        if endpoint.endswith("/chat/completions")
        else f"{endpoint}/chat/completions"
    )
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    def call(system: str, user: str) -> str:
        resp = httpx.post(
            url,
            headers=headers,
            timeout=timeout,
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"] or "")

    return call, model
