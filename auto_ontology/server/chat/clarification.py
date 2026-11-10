# SPDX-License-Identifier: Apache-2.0

"""How a clarification lives in a conversation: stored, recognised, answered.

The ``messages`` table has no column for structured parts, and adding one means
a Prisma migration for a feature that is off by default. Charts already travel
as a fenced block inside ``content``; a clarification does the same. The message
is the question in plain words followed by a ```clarification block holding the
JSON, so an older client still shows something readable.

The conversation itself is the state. A clarification is *pending* exactly when
it is the last message of its conversation: any later message (the answer, or a
new question) makes it stale. Nothing is held in memory, so it survives a
restart and cannot be resumed from another conversation or another user.
"""

from __future__ import annotations

import json
import re
from uuid import UUID

from pydantic import BaseModel, model_validator

from auto_ontology.retrieval.text_to_sql.clarification import (
    Clarification,
    ClarificationOption,
)

FENCE = "clarification"
_FENCE_RE = re.compile(rf"```{FENCE}\n(.*?)\n```", re.DOTALL)


class ClarificationAnswer(BaseModel):
    """The user's answer, sent on the request that follows a clarification."""

    clarification_id: UUID
    option_id: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _exactly_one_answer(self) -> "ClarificationAnswer":
        if self.text is not None:
            self.text = self.text.strip() or None
        if (self.option_id is None) == (self.text is None):
            raise ValueError("Send exactly one of option_id or text")
        return self


class ClarificationStateError(Exception):
    """The answer does not match the clarification pending in this conversation."""


def render_message(clarification: Clarification) -> str:
    """The ``content`` stored for the assistant message."""
    payload = json.dumps(clarification.model_dump(), ensure_ascii=False)
    return f"{clarification.question}\n\n```{FENCE}\n{payload}\n```"


def parse_message(content: str | None) -> Clarification | None:
    """The clarification stored in *content*, or ``None`` if it holds none.

    Lenient on purpose: content that merely looks similar, or a block that no
    longer validates, is ordinary text, never a pending question.
    """
    match = _FENCE_RE.search(content or "")
    if match is None:
        return None
    try:
        return Clarification.model_validate(json.loads(match.group(1)))
    except ValueError:
        return None


def history_text(content: str) -> str | None:
    """The plain text the model should see for a stored clarification message.

    ``None`` when *content* is not a clarification. The model sees the question
    and the readings that were offered, so that the answer turn that follows
    makes sense in the history.
    """
    clarification = parse_message(content)
    if clarification is None:
        return None
    options = "; ".join(option.label for option in clarification.options)
    return f"{clarification.question} Options: {options}"


def check_answer(pending: Clarification | None, answer: ClarificationAnswer) -> None:
    """Raise unless *answer* targets the clarification that is pending."""
    if pending is None or str(answer.clarification_id) != pending.clarification_id:
        raise ClarificationStateError(
            "This clarification is no longer pending in the conversation"
        )
    if answer.option_id is not None and _option(pending, answer.option_id) is None:
        raise ClarificationStateError("Unknown option for this clarification")


def _option(pending: Clarification, option_id: str) -> ClarificationOption | None:
    return next((o for o in pending.options if o.id == option_id), None)


def answer_text(
    pending: Clarification, answer: ClarificationAnswer | None, reply: str
) -> str:
    """The user's answer as words: the chosen option, or what they typed."""
    if answer is not None and answer.option_id is not None:
        option = _option(pending, answer.option_id)
        if option is not None:
            suffix = f" ({option.description})" if option.description else ""
            return f"{option.label}{suffix}"
    if answer is not None and answer.text:
        return answer.text
    return reply.strip()


def definition_evidence(pending: Clarification, answer: str) -> str:
    """The answer as authoritative evidence for the question it belongs to.

    Evidence is the pipeline's existing channel for facts the user states, so
    the answer reaches retrieval and SQL generation without touching either.
    """
    return f'The user defined "{pending.term}" as: {answer}'
