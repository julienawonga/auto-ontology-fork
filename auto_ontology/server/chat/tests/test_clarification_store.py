# SPDX-License-Identifier: Apache-2.0

"""A clarification against a real Postgres: stored, pending, answered, refused.

The conversation tables belong to the frontend's Prisma schema, which CI does
not create. These tests build the three tables they touch in a throwaway schema
and point the DAL at it, so no real conversation is ever read or written.
Skipped unless ``POSTGRES_*`` is set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

from auto_ontology.infra.postgres import get_postgres_connection_string  # noqa: E402
from auto_ontology.retrieval.text_to_sql.clarification import (  # noqa: E402
    Clarification,
    ClarificationOption,
)
from auto_ontology.server.chat import clarification as cl  # noqa: E402
from auto_ontology.server.chat import conversation_dal as dal  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_HOST"),
    reason="needs POSTGRES_* (a throwaway Postgres)",
)

ORIGINAL = "How many VIP customers do we have?"
ALICE, BOB = "alice", "bob"


@pytest.fixture(name="schema")
def schema_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    name = f"cltest_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_postgres_connection_string(), autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(
            f"""
            CREATE TABLE {name}.conversations (
                id uuid PRIMARY KEY, user_id text NOT NULL, title text,
                created_at timestamptz, updated_at timestamptz);
            CREATE TABLE {name}.messages (
                id uuid PRIMARY KEY, conversation_id uuid NOT NULL, role text NOT NULL,
                content text NOT NULL DEFAULT '', sql_code text, sql_response text,
                created_at timestamptz NOT NULL DEFAULT now());
            CREATE TABLE {name}.conversation_analytics (
                id uuid PRIMARY KEY, user_id text, source text, question text,
                question_timestamp timestamptz, response text, sql text,
                response_timestamp timestamptz);
            """
        )
    monkeypatch.setattr(dal, "FRONTEND_SCHEMA", name)
    try:
        yield name
    finally:
        with psycopg.connect(get_postgres_connection_string(), autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {name} CASCADE")


def _asked(conversation: uuid.UUID, user: str = ALICE) -> Clarification:
    """Run the first turn and store the question, as the pump does."""
    first = dal.prepare_conversation(
        conversation_id=conversation, user_id=user, question=ORIGINAL, source="test"
    )
    clarification = Clarification(
        clarification_id=str(uuid.uuid4()),
        term="VIP customers",
        question="What do you mean by VIP customers?",
        options=[
            ClarificationOption(id="opt-1", label="Above the average"),
            ClarificationOption(id="opt-2", label="Top 10%"),
        ],
        original_question=ORIGINAL,
    )
    dal.persist_clarification_message(
        conversation_id=conversation,
        user_id=user,
        analytics_id=first.analytics_id,
        clarification=clarification,
    )
    return clarification


def _messages(schema: str, conversation: uuid.UUID) -> list[tuple[str, str]]:
    with psycopg.connect(get_postgres_connection_string()) as conn:
        return conn.execute(
            f"SELECT role, content FROM {schema}.messages "
            "WHERE conversation_id = %s ORDER BY created_at, id",
            (str(conversation),),
        ).fetchall()


def test_the_question_is_stored_as_an_assistant_message(schema: str) -> None:
    conversation = uuid.uuid4()
    clarification = _asked(conversation)

    (user_row, assistant_row) = _messages(schema, conversation)

    assert user_row == ("user", ORIGINAL)
    assert assistant_row[0] == "assistant"
    assert assistant_row[1].startswith("What do you mean by VIP customers?")
    assert cl.parse_message(assistant_row[1]) == clarification


def test_the_question_completes_its_analytics_row(schema: str) -> None:
    _asked(uuid.uuid4())

    with psycopg.connect(get_postgres_connection_string()) as conn:
        (row,) = conn.execute(
            f"SELECT response, sql FROM {schema}.conversation_analytics"
        ).fetchall()

    assert row == ("What do you mean by VIP customers?", None)


def test_a_valid_answer_finds_the_pending_question_and_is_stored(schema: str) -> None:
    conversation = uuid.uuid4()
    clarification = _asked(conversation)

    prepared = dal.prepare_conversation(
        conversation_id=conversation,
        user_id=ALICE,
        question="Top 10%",
        source="test",
        clarification=cl.ClarificationAnswer(
            clarification_id=clarification.clarification_id, option_id="opt-2"
        ),
    )

    assert prepared.pending_clarification == clarification
    # The history the model gets holds the question asked and the options offered.
    assert prepared.history[-1].question == ORIGINAL
    assert "Options: Above the average; Top 10%" in prepared.history[-1].response
    assert _messages(schema, conversation)[-1] == ("user", "Top 10%")


def test_a_free_text_message_sees_the_pending_question(schema: str) -> None:
    conversation = uuid.uuid4()
    clarification = _asked(conversation)

    prepared = dal.prepare_conversation(
        conversation_id=conversation,
        user_id=ALICE,
        question="top ten percent",
        source="t",
    )

    assert prepared.pending_clarification == clarification


def test_a_stale_id_is_refused_and_leaves_nothing_behind(schema: str) -> None:
    conversation = uuid.uuid4()
    _asked(conversation)
    before = _messages(schema, conversation)

    with pytest.raises(cl.ClarificationStateError):
        dal.prepare_conversation(
            conversation_id=conversation,
            user_id=ALICE,
            question="x",
            source="t",
            clarification=cl.ClarificationAnswer(
                clarification_id=uuid.uuid4(), text="x"
            ),
        )

    assert _messages(schema, conversation) == before


def test_an_answered_question_is_no_longer_pending(schema: str) -> None:
    conversation = uuid.uuid4()
    clarification = _asked(conversation)
    answer = cl.ClarificationAnswer(
        clarification_id=clarification.clarification_id, option_id="opt-1"
    )
    dal.prepare_conversation(
        conversation_id=conversation,
        user_id=ALICE,
        question="Above the average",
        source="t",
        clarification=answer,
    )

    with pytest.raises(cl.ClarificationStateError):
        dal.prepare_conversation(
            conversation_id=conversation,
            user_id=ALICE,
            question="Above the average",
            source="t",
            clarification=answer,
        )


def test_another_conversation_cannot_use_the_id(schema: str) -> None:
    clarification = _asked(uuid.uuid4())
    other = uuid.uuid4()
    dal.prepare_conversation(
        conversation_id=other, user_id=ALICE, question="Something else", source="t"
    )

    with pytest.raises(cl.ClarificationStateError):
        dal.prepare_conversation(
            conversation_id=other,
            user_id=ALICE,
            question="x",
            source="t",
            clarification=cl.ClarificationAnswer(
                clarification_id=clarification.clarification_id, text="x"
            ),
        )


def test_another_user_cannot_answer_in_the_conversation(schema: str) -> None:
    conversation = uuid.uuid4()
    clarification = _asked(conversation)

    with pytest.raises(dal.ConversationAccessError):
        dal.prepare_conversation(
            conversation_id=conversation,
            user_id=BOB,
            question="x",
            source="t",
            clarification=cl.ClarificationAnswer(
                clarification_id=clarification.clarification_id, text="x"
            ),
        )
