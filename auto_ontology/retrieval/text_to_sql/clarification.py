# SPDX-License-Identifier: Apache-2.0

"""Decide whether to ask the user what a business term means before writing SQL.

"How many VIP customers do we have?" has no answer until "VIP" is defined, and a
model that is not asked will invent a definition and answer confidently. This
module asks instead, but only when that is clearly the better move: asking too
often is the failure of interactive systems, so every rule here errs towards
answering.

The detector runs once per question, before the graph, and never blocks:
whatever goes wrong (no retriever, a failed search, an unusable model reply) the
decision is "do not ask" and the pipeline answers as it always did.

It is deliberately not built on ``retrieval/interactive``. That package is a
multi-round benchmark loop: it keeps a session across turns, produces a free-text
question, and judges a term unresolved against an external knowledge base this
product does not have. Here there is exactly one round, the evidence is the
ontology the chat already searches, and the question must come with typed
options.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from auto_ontology.catalog.constants import Labels
from auto_ontology.retrieval.data_access.semantic_search import search_semantic_index
from auto_ontology.semantic.constants import (
    LABEL_COLUMN_ATTRIBUTE,
    LABEL_SQL_ATTRIBUTE,
    LABEL_TERM,
)
from auto_ontology.utils.llm_invoke import invoke_with_structured_output

logger = logging.getLogger(__name__)

MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_TERM_CHARS = 80
MAX_QUESTION_CHARS = 300
MAX_LABEL_CHARS = 120
MAX_DESCRIPTION_CHARS = 240

# Hits are L2 vector distances: lower is closer.
_SNIPPET_CHARS = 300

# A candidate answer that says nothing. The last option must never be one, and
# the UI adds its own "Other" free-text entry, so these are dropped, not kept.
_VAGUE_LABEL = re.compile(
    r"^\s*(other|others|something else|anything else|none of (the|these) above|"
    r"none of these|not sure|i don'?t know|n/?a|autre|autres)\b[\s.!?]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClarificationConfig:
    """Thresholds of the detector. Defaults are conservative; none is tuned yet."""

    # Only a certified SqlAttribute or human-written CustomAnalysis this close
    # to the question counts as verified. 0.62 is the distance the interactive
    # package already uses for "this term is covered".
    definition_max_distance: float = 0.62
    # Hits farther than this are not shown to the model as possibly related.
    related_max_distance: float = 0.75
    # Questions shorter than this are too thin to judge (a greeting, one word).
    min_question_chars: int = 12
    schema_hits: int = 8
    definition_hits: int = 3


DEFAULT_CONFIG = ClarificationConfig()


def _default_certified(attr_id: str) -> bool | None:
    """Read SqlAttribute certification from the store; return None on failure."""
    try:
        from auto_ontology.dal.sql_attributes import get_full_sql_attribute_by_id

        row = get_full_sql_attribute_by_id(attr_id)
    except Exception:
        logger.debug(
            "clarification certification lookup failed for %s", attr_id, exc_info=True
        )
        return None
    if not row or row.get("certified") is None:
        return None
    return bool(row["certified"])


class ClarificationOption(BaseModel):
    """One concrete reading of the term the user can pick."""

    id: str
    label: str
    description: str | None = None


class Clarification(BaseModel):
    """A question for the user, with the readings to choose from.

    ``original_question`` is kept server-side so that the answer, which arrives
    on a later request, can be merged back into the question it belongs to. It
    is not part of the SSE event.
    """

    clarification_id: str
    term: str
    question: str
    options: list[ClarificationOption]
    allow_other: bool = True
    original_question: str = ""

    def event(self) -> dict[str, Any]:
        """The SSE event: everything the client needs, nothing it must echo."""
        return {
            "type": "clarification",
            "clarification_id": self.clarification_id,
            "term": self.term,
            "question": self.question,
            "options": [o.model_dump(exclude_none=True) for o in self.options],
            "allow_other": self.allow_other,
        }


@dataclass(frozen=True)
class ClarificationDecision:
    """The outcome, with a stable ``reason`` code that is always logged."""

    clarification: Clarification | None
    reason: str


# Plain BaseModel, not StrictModel: models routinely emit "" for an absent field
# and StrictModel rejects empty strings, which would discard the whole reply.
class _ProposedOption(BaseModel):
    label: str
    description: str | None = None


class ClarificationProposalModel(BaseModel):
    """Structured reply of the detector's single model call."""

    reasoning: str = Field(
        ..., description="One or two sentences: why this does or does not need asking."
    )
    kind: Literal[
        "undefined_business_term", "schema_or_value_reference", "fully_specified"
    ]
    term: str | None = Field(
        default=None,
        description="The exact undefined phrase from the question, when there is one.",
    )
    covered_by_definition: bool = Field(
        ...,
        description="True when a retrieved definition already says what the term means.",
    )
    changes_sql_materially: bool = Field(
        ...,
        description="True when the readings of the term lead to clearly different SQL.",
    )
    question: str | None = Field(
        default=None, description="One short question asking what the term means."
    )
    options: list[_ProposedOption] = Field(
        default_factory=list,
        description="2 to 4 distinct, concrete readings of the term.",
    )


class _AnswerCheckModel(BaseModel):
    is_answer: bool = Field(
        ...,
        description="True when the reply answers the clarification question.",
    )


_PROMPT = """\
You decide whether a text-to-SQL assistant must ask the user ONE clarification
question before writing SQL. Asking costs the user a turn, so ask only when
guessing would be worse.

Ask ONLY when ALL of these hold:
1. The question contains a business term that names a concept the data does not
   define, such as "VIP customer", "active user", "churned", "high value",
   "best seller" (kind = undefined_business_term).
2. No retrieved definition below states what that term means
   (covered_by_definition = false).
3. The plausible readings give clearly different SQL, such as a different
   threshold, ranking or set of rows (changes_sql_materially = true).

Do NOT ask when:
- The term is a table, column, status or value that appears in the schema or
  values below, even informally ("rentals", "late returns", "store 2")
  (kind = schema_or_value_reference).
- The question gives its own definition or threshold, or is fully determined
  (kind = fully_specified).
- A definition below covers the term (covered_by_definition = true).
- You would only be choosing a sort order, a date format or a column name.

When you do ask, write the question in the language of the user's question, about
the business concept and never about tables or columns, and give 2 to 4 options.
Each option is one concrete, different reading of the term that the schema below
can express, with a short label and an optional one-line description. Never add
a vague option such as "other" or "something else": the interface adds its own
free-text choice. When you do not ask, leave question and options empty.

RELATED DEFINITIONS (business definitions, saved analyses, glossary terms; not all are verified):
{definitions}

SCHEMA AND VALUES (closest columns to the question):
{schema}

CONVERSATION SO FAR:
{history}

SUPPLIED EVIDENCE:
{evidence}

QUESTION:
{question}
"""

_ANSWER_PROMPT = """\
An assistant asked the user a clarification question and offered options. Decide
whether the user's next message answers it, or is instead a new, unrelated
question. A short phrase, a choice, a number or a definition is an answer.

Clarification question: {question}
Options: {options}

User message: {reply}
"""


def validate_options(
    proposed: list[_ProposedOption],
) -> list[ClarificationOption] | None:
    """Return 2 to 4 distinct, non-vague, bounded options, or ``None`` if unusable.

    Vague entries and duplicates are dropped rather than failing the whole
    proposal; a count outside 2..4 after that, or an over-long text, is not
    repaired because it means the model did not follow the contract.
    """
    cleaned: list[ClarificationOption] = []
    seen: set[str] = set()
    for item in proposed:
        label = " ".join((item.label or "").split())
        if not label or _VAGUE_LABEL.match(label):
            continue
        key = label.casefold()
        if key in seen:
            continue
        description = " ".join((item.description or "").split()) or None
        if len(label) > MAX_LABEL_CHARS or (
            description is not None and len(description) > MAX_DESCRIPTION_CHARS
        ):
            return None
        seen.add(key)
        cleaned.append(
            ClarificationOption(
                id=f"opt-{len(cleaned) + 1}", label=label, description=description
            )
        )
    if not MIN_OPTIONS <= len(cleaned) <= MAX_OPTIONS:
        return None
    return cleaned


def _snippet(hit: dict) -> str:
    text = " ".join(str(hit.get("text") or "").split())
    return text[:_SNIPPET_CHARS]


def _distance(hit: dict) -> float:
    score = hit.get("score")
    return float(score) if score is not None else float("inf")


def _search(
    retriever: object, question: str, label: str, k: int, target_db: str | None
) -> list[dict]:
    # Not ``_search_by_label``: that helper turns a failed search into an empty
    # list, and here "no definition found" would then read as "no definition
    # exists" and trigger a question. A failure must reach ``detect``'s handler.
    return list(
        search_semantic_index(
            retriever,
            question,
            label_filter=[label],
            per_label_k=k,
            database_name=target_db,
        )
    )


def _format_hits(hits: list[dict], empty: str) -> str:
    if not hits:
        return empty
    return "\n".join(
        f"- [{h.get('label', '?')}, distance {_distance(h):.2f}] {_snippet(h)}"
        for h in hits
    )


def _format_history(history: list[dict[str, str | None]]) -> str:
    if not history:
        return "None"
    lines = []
    for turn in history[-3:]:
        lines.append(f"User: {(turn.get('question') or '').strip()[:300]}")
        lines.append(f"Assistant: {(turn.get('response') or '').strip()[:300]}")
    return "\n".join(lines)


def _language_note(answer_language: str | None, translated: str, original: str) -> str:
    """Prompt suffix that explicitly names the clarification answer language."""
    if not answer_language:
        return ""
    translation_note = ""
    if translated.strip() != original.strip():
        translation_note = (
            f"\n\nThe question is written in {answer_language}; its translation into the "
            f"ontology's language, used for searching, is: {translated.strip()}"
        )
    return (
        f"{translation_note}\n\nWrite the clarification question and the option labels "
        f"in {answer_language}."
    )


def _decide(
    *,
    question: str,
    evidence: str,
    llm: BaseChatModel | None,
    semantic_retriever: object | None,
    target_db: str | None,
    history: list[dict[str, str | None]],
    config: ClarificationConfig,
    retrieval_question: str | None = None,
    answer_language: str | None = None,
    certified_lookup: Callable[[str], bool | None] | None = None,
) -> ClarificationDecision:
    if len(question.strip()) < config.min_question_chars:
        return ClarificationDecision(None, "skipped_question_too_short")
    if evidence.strip():
        # Evidence is what the caller supplies as authoritative; it is the
        # caller's way of having already answered.
        return ClarificationDecision(None, "skipped_evidence_supplied")
    if llm is None:
        return ClarificationDecision(None, "skipped_no_llm")
    if semantic_retriever is None:
        return ClarificationDecision(None, "skipped_no_semantic_index")

    # With question translation on, the English text is what the English ontology
    # is searched with; the prompt below still shows the user's own wording.
    search_question = (retrieval_question or "").strip() or question
    definitions = sorted(
        [
            *_search(
                semantic_retriever,
                search_question,
                LABEL_SQL_ATTRIBUTE,
                config.definition_hits,
                target_db,
            ),
            *_search(
                semantic_retriever,
                search_question,
                Labels.CUSTOM_ANALYSIS,
                config.definition_hits,
                target_db,
            ),
            *_search(
                semantic_retriever,
                search_question,
                LABEL_TERM,
                config.definition_hits,
                target_db,
            ),
        ],
        key=_distance,
    )
    lookup = certified_lookup or _default_certified
    eligible = []
    for hit in definitions:
        if _distance(hit) > config.definition_max_distance:
            continue
        if hit.get("label") == Labels.CUSTOM_ANALYSIS:
            eligible.append(hit)
        elif (
            hit.get("label") == LABEL_SQL_ATTRIBUTE
            and lookup(str(hit.get("id") or "")) is True
        ):
            eligible.append(hit)
    if eligible:
        verified_hit = min(eligible, key=_distance)
        logger.info(
            "Clarification skipped: verified definition within %.2f: %s",
            config.definition_max_distance,
            _format_hits([verified_hit], ""),
        )
        return ClarificationDecision(None, "skipped_verified_definition")
    if definitions and _distance(definitions[0]) <= config.definition_max_distance:
        logger.info(
            "Clarification: closest definition is not certified, asking the model: %s",
            _format_hits(definitions[:1], ""),
        )
    related = [h for h in definitions if _distance(h) <= config.related_max_distance]
    schema = _search(
        semantic_retriever,
        search_question,
        LABEL_COLUMN_ATTRIBUTE,
        config.schema_hits,
        target_db,
    )

    proposal = invoke_with_structured_output(
        llm,
        [
            HumanMessage(
                content=_PROMPT.format(
                    definitions=_format_hits(related, "None found."),
                    schema=_format_hits(schema, "None found."),
                    history=_format_history(history),
                    evidence=evidence.strip() or "None",
                    question=question.strip(),
                )
                + _language_note(answer_language, search_question, question)
            )
        ],
        ClarificationProposalModel,
    )
    if proposal is None:
        return ClarificationDecision(None, "llm_no_result")
    logger.info(
        "Clarification proposal: kind=%s term=%r covered=%s material=%s reasoning=%s",
        proposal.kind,
        proposal.term,
        proposal.covered_by_definition,
        proposal.changes_sql_materially,
        proposal.reasoning,
    )

    if proposal.kind == "schema_or_value_reference":
        return ClarificationDecision(None, "llm_schema_reference")
    if proposal.kind == "fully_specified":
        return ClarificationDecision(None, "llm_fully_specified")
    if proposal.covered_by_definition:
        return ClarificationDecision(None, "llm_covered_by_definition")
    if not proposal.changes_sql_materially:
        return ClarificationDecision(None, "llm_not_material")

    term = " ".join((proposal.term or "").split())
    text = " ".join((proposal.question or "").split())
    options = validate_options(proposal.options)
    if (
        not term
        or len(term) > MAX_TERM_CHARS
        or not text
        or len(text) > MAX_QUESTION_CHARS
        or options is None
    ):
        return ClarificationDecision(None, "invalid_proposal")

    return ClarificationDecision(
        Clarification(
            clarification_id=str(uuid.uuid4()),
            term=term,
            question=text,
            options=options,
            original_question=question,
        ),
        "asked",
    )


def detect_clarification(
    *,
    question: str,
    evidence: str = "",
    llm: BaseChatModel | None,
    semantic_retriever: object | None,
    target_db: str | None = None,
    history: list[dict[str, str | None]] | None = None,
    config: ClarificationConfig = DEFAULT_CONFIG,
    retrieval_question: str | None = None,
    answer_language: str | None = None,
    certified_lookup: Callable[[str], bool | None] | None = None,
) -> ClarificationDecision:
    """Return the clarification to ask, or why not. Never raises."""
    try:
        decision = _decide(
            question=question,
            evidence=evidence,
            llm=llm,
            semantic_retriever=semantic_retriever,
            target_db=target_db,
            history=history or [],
            config=config,
            retrieval_question=retrieval_question,
            answer_language=answer_language,
            certified_lookup=certified_lookup,
        )
    except Exception:  # noqa: BLE001 - a detector failure must never fail the chat
        logger.exception("Clarification detector failed; answering without asking")
        return ClarificationDecision(None, "detector_error")
    logger.info(
        "Clarification decision: %s (term=%r)",
        decision.reason,
        decision.clarification.term if decision.clarification else None,
    )
    return decision


def is_clarification_answer(
    *, clarification: Clarification, reply: str, llm: BaseChatModel | None
) -> bool:
    """Whether a free-text reply answers *clarification* (vs. a new question).

    Used only when the client sent no structured answer. Failing open (``True``)
    matches the contract that a plain message is the answer; the cost of the
    wrong guess is one extra definition in the evidence, not a lost question.
    """
    if llm is None:
        return True
    try:
        result = invoke_with_structured_output(
            llm,
            [
                HumanMessage(
                    content=_ANSWER_PROMPT.format(
                        question=clarification.question,
                        options="; ".join(o.label for o in clarification.options),
                        reply=reply.strip(),
                    )
                )
            ],
            _AnswerCheckModel,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Clarification answer check failed; treating reply as answer")
        return True
    return True if result is None else result.is_answer


__all__ = [
    "Clarification",
    "ClarificationConfig",
    "ClarificationDecision",
    "ClarificationOption",
    "DEFAULT_CONFIG",
    "detect_clarification",
    "is_clarification_answer",
    "validate_options",
]
