# SPDX-License-Identifier: Apache-2.0

"""A clarification as it travels through the chat server.

Covers the stored message shape, how a later request is matched to the pending
question (valid, stale, foreign, free text), the history the model sees, the
SSE stream that ends after the question, and the worker's switch.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from pydantic import ValidationError

from auto_ontology.retrieval.text_to_sql.clarification import (
    Clarification,
    ClarificationDecision,
    ClarificationOption,
)
from auto_ontology.server.chat import clarification as cl
from auto_ontology.server.chat import router
from auto_ontology.server.chat import worker as worker_module
from auto_ontology.server.chat.conversation_dal import _rows_to_history
from auto_ontology.server.chat.helpers import ChatRequest


def _clarification(**overrides: Any) -> Clarification:
    fields: dict[str, Any] = {
        "clarification_id": str(uuid.uuid4()),
        "term": "VIP customers",
        "question": "What do you mean by VIP customers?",
        "options": [
            ClarificationOption(id="opt-1", label="Above the average payments"),
            ClarificationOption(
                id="opt-2", label="Top 10% by payments", description="Ranked"
            ),
        ],
        "allow_other": True,
        "original_question": "How many VIP customers do we have?",
    }
    fields.update(overrides)
    return Clarification(**fields)


# -- stored message ---------------------------------------------------------


def test_message_round_trips_through_its_stored_form() -> None:
    original = _clarification()

    content = cl.render_message(original)

    assert content.startswith(
        "What do you mean by VIP customers?\n\n```clarification\n"
    )
    assert cl.parse_message(content) == original


def test_message_keeps_non_ascii_text_readable() -> None:
    original = _clarification(question="Que voulez-vous dire par « client VIP » ?")

    assert "« client VIP »" in cl.render_message(original)
    assert cl.parse_message(cl.render_message(original)) == original


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "An ordinary answer",
        "```chart\n{}\n```",
        "```clarification\nnot json\n```",
        '```clarification\n{"term": "x"}\n```',
    ],
)
def test_content_that_is_not_a_valid_clarification_is_ordinary_text(
    content: str | None,
) -> None:
    assert cl.parse_message(content) is None
    assert cl.history_text(content or "") is None


def test_the_model_sees_the_question_and_the_options_offered() -> None:
    text = cl.history_text(cl.render_message(_clarification()))

    assert text == (
        "What do you mean by VIP customers? "
        "Options: Above the average payments; Top 10% by payments"
    )


def _row(role: str, content: str) -> dict:
    return {"role": role, "content": content, "sql_code": None, "sql_response": None}


def test_history_contains_both_the_question_and_the_answer() -> None:
    rows = [
        _row("user", "How many VIP customers do we have?"),
        _row("assistant", cl.render_message(_clarification())),
        _row("user", "Top 10% by payments"),
        _row("assistant", "There are 60 VIP customers."),
    ]

    history = _rows_to_history(rows)

    assert [t.question for t in history] == [
        "How many VIP customers do we have?",
        "Top 10% by payments",
    ]
    assert history[0].response.startswith("What do you mean by VIP customers?")
    assert "```" not in history[0].response
    assert history[1].response == "There are 60 VIP customers."


# -- request model ----------------------------------------------------------


def test_request_accepts_an_option_or_free_text_answer() -> None:
    cid = str(uuid.uuid4())

    by_option = ChatRequest(
        question="Top 10%",
        clarification={"clarification_id": cid, "option_id": "opt-2"},
    )
    by_text = ChatRequest(
        question="above 100",
        clarification={"clarification_id": cid, "text": " above 100 "},
    )

    assert by_option.clarification.option_id == "opt-2"
    assert by_text.clarification.text == "above 100"


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"option_id": "opt-1", "text": "both"},
        {"text": "   "},
        {"clarification_id": "not-a-uuid", "option_id": "opt-1"},
    ],
)
def test_request_rejects_an_unusable_answer(answer: dict[str, Any]) -> None:
    if "clarification_id" not in answer:
        answer = {"clarification_id": str(uuid.uuid4()), **answer}
    with pytest.raises(ValidationError):
        ChatRequest(question="x", clarification=answer)


def test_a_plain_request_is_unchanged() -> None:
    request = ChatRequest(question="How many orders?")

    assert request.clarification is None
    assert request.supports_clarification is False


# -- matching the answer to the pending question ----------------------------


def _answer(pending: Clarification, **fields: Any) -> cl.ClarificationAnswer:
    return cl.ClarificationAnswer(
        clarification_id=fields.pop("clarification_id", pending.clarification_id),
        **fields,
    )


def test_a_valid_option_answer_is_accepted() -> None:
    pending = _clarification()

    cl.check_answer(pending, _answer(pending, option_id="opt-2"))
    cl.check_answer(pending, _answer(pending, text="more than 100"))


def test_a_stale_id_is_refused() -> None:
    pending = _clarification()

    with pytest.raises(cl.ClarificationStateError):
        cl.check_answer(
            pending, _answer(pending, clarification_id=uuid.uuid4(), text="x")
        )


def test_nothing_pending_means_any_id_is_refused() -> None:
    """A foreign conversation, or one already answered, has no pending question."""
    with pytest.raises(cl.ClarificationStateError):
        cl.check_answer(
            None,
            cl.ClarificationAnswer(clarification_id=uuid.uuid4(), text="x"),
        )


def test_an_option_that_was_never_offered_is_refused() -> None:
    pending = _clarification()

    with pytest.raises(cl.ClarificationStateError):
        cl.check_answer(pending, _answer(pending, option_id="opt-9"))


def test_the_answer_becomes_evidence_for_the_original_question() -> None:
    pending = _clarification()

    by_option = cl.answer_text(pending, _answer(pending, option_id="opt-2"), "Top 10%")
    by_text = cl.answer_text(pending, _answer(pending, text="over 100"), "over 100")
    free = cl.answer_text(pending, None, "  the top ten percent ")

    assert by_option == "Top 10% by payments (Ranked)"
    assert by_text == "over 100"
    assert free == "the top ten percent"
    assert cl.definition_evidence(pending, by_text) == (
        'The user defined "VIP customers" as: over 100'
    )


# -- the stream: persisted, then [DONE] ------------------------------------


class _FakeWorker:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def events(self) -> Iterator[dict[str, Any]]:
        return iter(self._events)


def _slot(events: list[dict[str, Any]], conversation: bool = True) -> router._Slot:
    return router._Slot(
        key="user:conv",
        worker=_FakeWorker(events),  # type: ignore[arg-type]
        user_id="user",
        conversation_id=uuid.uuid4() if conversation else None,
        analytics_id="analytics",
        question="How many VIP customers do we have?",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
    )


@pytest.fixture(name="stored")
def stored_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        router, "persist_clarification_message", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(
        router, "persist_assistant_result", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(router, "_release", lambda _slot: None)
    return calls


def test_the_stream_carries_the_question_then_ends(
    stored: list[dict[str, Any]],
) -> None:
    event = _clarification().event()
    slot = _slot([{"type": "step", "node": "x", "phase": "start"}, event])

    router._pump(slot)
    frames = [f for f in router._stream_slot(slot) if not f.startswith(":")]

    assert frames[-1] == "data: [DONE]\n\n"
    assert frames[-2] == router._sse(event)
    # Nothing after the question: no result, no charts, no error.
    assert not any('"type": "result"' in f or '"type": "charts"' in f for f in frames)


def test_the_question_is_stored_with_the_original_question(
    stored: list[dict[str, Any]],
) -> None:
    clarification = _clarification()
    slot = _slot([clarification.event()])

    router._pump(slot)

    assert len(stored) == 1
    persisted = stored[0]["clarification"]
    assert persisted.clarification_id == clarification.clarification_id
    assert persisted.options == clarification.options
    # The event does not carry it; the pump supplies the question that was asked.
    assert persisted.original_question == "How many VIP customers do we have?"
    assert stored[0]["analytics_id"] == "analytics"


def test_a_storage_failure_does_not_break_the_stream(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    def broken(**kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(router, "persist_clarification_message", broken)
    slot = _slot([_clarification().event()])

    router._pump(slot)

    assert any('"clarification"' in f for f in router._stream_slot(slot))


# -- the worker's switch ----------------------------------------------------


class _Queue:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.puts: list[Any] = []

    def get(self) -> Any:
        if self._items:
            return self._items.pop(0)
        raise EOFError

    def put(self, item: Any) -> None:
        self.puts.append(item)


def _run_worker(
    monkeypatch: pytest.MonkeyPatch,
    payload: tuple,
    clarification: Clarification | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Drive one ask; return what the worker emitted and what the fakes saw."""
    monkeypatch.setattr(
        "auto_ontology.utils.llm_invoke.get_llm_client", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "auto_ontology.utils.llm_invoke.get_non_reasoning_llm_client",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("auto_ontology.connectors.get_connectors", lambda: ["c"])
    monkeypatch.setattr(
        "auto_ontology.utils.get_data_objects_retriever", lambda: "data"
    )
    monkeypatch.setattr(
        "auto_ontology.utils.get_semantic_objects_retriever", lambda: "semantic"
    )
    monkeypatch.setattr(
        "auto_ontology.server.chat.settings_dal.fetch_acronyms", lambda: []
    )
    monkeypatch.setattr(
        "auto_ontology.server.chat.settings_dal.fetch_custom_prompts", lambda: ""
    )
    seen: dict[str, Any] = {"detector": 0, "agent": 0}

    def fake_detect(**kwargs: Any) -> ClarificationDecision:
        seen["detector"] += 1
        seen["detector_kwargs"] = kwargs
        return ClarificationDecision(
            clarification, "asked" if clarification else "llm_not_material"
        )

    def fake_stream(agent_payload: dict) -> Iterator[dict]:
        seen["agent"] += 1
        return iter([{"type": "result", "answer": {"response": "done"}}])

    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.clarification.detect_clarification",
        fake_detect,
    )
    monkeypatch.setattr(
        "auto_ontology.retrieval.text_to_sql.main.stream_agent_response", fake_stream
    )
    out = _Queue([])
    worker_module._worker_loop(_Queue([(worker_module._MSG_ASK, payload)]), out)
    return out.puts, seen


def test_worker_ends_the_turn_with_the_question_and_does_not_run_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarification = _clarification()

    puts, seen = _run_worker(
        monkeypatch,
        ("How many VIP customers do we have?", None, None, None, [], None, True),
        clarification,
    )

    assert seen["agent"] == 0
    assert (worker_module._TAG_EVENT, clarification.event()) in puts
    assert puts[-1] == (worker_module._TAG_DONE, None)
    assert seen["detector_kwargs"]["question"] == "How many VIP customers do we have?"
    assert seen["detector_kwargs"]["semantic_retriever"] == "semantic"


def test_worker_runs_the_agent_when_the_detector_does_not_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    puts, seen = _run_worker(monkeypatch, ("q", None, None, None, [], None, True), None)

    assert seen["detector"] == 1
    assert seen["agent"] == 1
    assert any(tag == worker_module._TAG_EVENT for tag, _ in puts)


@pytest.mark.parametrize(
    "payload",
    [
        ("q", None, None, None, [], None, False),
        ("q", None, None, None, [], None),
    ],
    ids=["switch off", "older six-field message"],
)
def test_worker_never_consults_the_detector_unless_asked_to(
    monkeypatch: pytest.MonkeyPatch, payload: tuple
) -> None:
    _, seen = _run_worker(monkeypatch, payload, _clarification())

    assert seen["detector"] == 0
    assert seen["agent"] == 1


def test_submit_forwards_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    fake = SimpleNamespace(_in_q=SimpleNamespace(put=sent.append))

    worker_module.PrewarmedWorker.submit(fake, "q", detect_clarification=True)  # type: ignore[arg-type]
    worker_module.PrewarmedWorker.submit(fake, "q")  # type: ignore[arg-type]

    assert sent[0][1][-1] is True
    # Off is the original six-field message, so older tests and senders agree.
    assert len(sent[1][1]) == 6
