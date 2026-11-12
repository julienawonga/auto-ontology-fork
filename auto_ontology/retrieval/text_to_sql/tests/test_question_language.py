# SPDX-License-Identifier: Apache-2.0

"""Question translation (QUESTION_TRANSLATION_ENABLED), tested with fakes."""

from __future__ import annotations

import importlib
import json
import logging

from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from auto_ontology.retrieval.text_to_sql import question_language as ql
from auto_ontology.retrieval.text_to_sql.agents import sql_from_semantic
from auto_ontology.retrieval.text_to_sql.agents.sql_from_semantic import (
    SQLFromCandidatesAgent,
)
from auto_ontology.retrieval.text_to_sql.models import SQLGenerationModel
from auto_ontology.retrieval.text_to_sql.state import (
    TextToSQLPayload,
    get_original_question,
    get_question_for_processing,
)
from auto_ontology.utils import llm_invoke

FRENCH = "Combien de commandes avons-nous reçues en mars ?"
ENGLISH = "How many orders did we receive in March?"


class FakeLLM:
    """Returns a canned reply and records every call."""

    def __init__(self, reply: Any = None, *, boom: bool = False) -> None:
        self.reply = reply
        self.boom = boom
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> SimpleNamespace:
        self.calls.append(messages)
        if self.boom:
            raise RuntimeError("gateway down")
        return SimpleNamespace(content=self.reply)


def _reply(language: str = "fr", translation: str = ENGLISH) -> str:
    return json.dumps({"language": language, "translation": translation})


# --- detection heuristic ----------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "How many customers do we have?",
        "Top 5 artists by revenue",
        "Show me sales for each year.",
        "revenue 2023",
        "",
    ],
)
def test_plain_english_is_recognised_without_the_model(question: str) -> None:
    assert ql.looks_english(question)
    llm = FakeLLM(boom=True)
    result = ql.resolve_question_language(question, llm)
    assert not llm.calls
    assert result is None or (result.language == "en" and not result.translated)


@pytest.mark.parametrize(
    "question",
    [
        FRENCH,
        "Quels sont les clients par pays",
        "Cuantos clientes hay por pais",
        "Wie viele Kunden haben wir",
        "Quel est le chiffre d'affaires",
        "café revenue",
    ],
)
def test_other_languages_and_accents_are_not_confidently_english(
    question: str,
) -> None:
    assert not ql.looks_english(question)


# --- translation parsing ----------------------------------------------------


def test_translation_returns_language_and_english() -> None:
    llm = FakeLLM(_reply())
    result = ql.resolve_question_language(FRENCH, llm)
    assert result == ql.QuestionLanguage("fr", "en", ENGLISH, True)
    assert len(llm.calls) == 1
    prompt = llm.calls[0][0].content
    assert FRENCH in prompt
    assert "ISO 639-1" in prompt


def test_fenced_json_and_chatter_are_tolerated() -> None:
    llm = FakeLLM("Sure!\n```json\n" + _reply() + "\n```")
    assert ql.resolve_question_language(FRENCH, llm) is not None


def test_model_saying_english_leaves_the_question_alone() -> None:
    llm = FakeLLM(_reply("en", "whatever"))
    result = ql.resolve_question_language("café sales per country", llm)
    assert result == ql.QuestionLanguage("en", "en", "café sales per country", False)


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "{",
        "[]",
        json.dumps({"language": "fr"}),
        json.dumps({"language": "French please", "translation": ENGLISH}),
        json.dumps({"language": "fr", "translation": ""}),
        json.dumps({"language": "fr", "translation": 5}),
        json.dumps({"language": "fr", "translation": "x" * 5000}),
        None,
    ],
)
def test_malformed_output_falls_back_to_the_original(
    reply: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert ql.resolve_question_language(FRENCH, FakeLLM(reply)) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_llm_failure_or_missing_llm_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert ql.resolve_question_language(FRENCH, FakeLLM(boom=True)) is None
        assert ql.resolve_question_language(FRENCH, None) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) >= 2


def test_content_blocks_reply_is_read() -> None:
    llm = FakeLLM([{"type": "text", "text": _reply()}])
    assert ql.resolve_question_language(FRENCH, llm) is not None


# --- state: retrieval gets English, generation gets both --------------------


@pytest.fixture(name="main")
def main_fixture(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(llm_invoke, "get_llm_client", lambda **_k: None)
    monkeypatch.setattr(llm_invoke, "get_non_reasoning_llm_client", lambda **_k: None)
    return importlib.import_module("auto_ontology.retrieval.text_to_sql.main")


def _payload(question: str = FRENCH) -> TextToSQLPayload:
    return cast(
        TextToSQLPayload,
        {
            "question": question,
            "data_retriever": object(),
            "connectors": [SimpleNamespace(dialect="sqlite", database_name="db")],
        },
    )


def _run_generation(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> list[tuple[str, Any]]:
    captured: list[Any] = []

    def fake_invoke(_llm: Any, messages: list[Any], _schema: Any) -> SQLGenerationModel:
        captured.extend(messages)
        return SQLGenerationModel(thought="t", sql_code="SELECT 1", response="ok")

    monkeypatch.setattr(
        sql_from_semantic, "safe_invoke_with_structured_output", fake_invoke
    )
    state["path_state"].update(
        primary_attribute=None, relevant_tables=[], relevant_queries=[]
    )
    SQLFromCandidatesAgent().execute(cast(Any, state))
    return [(type(m).__name__, m.content) for m in captured]


def test_enabled_retrieval_reads_english_and_generation_reads_both(
    main: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(ql.DISABLED_ENV, raising=False)
    llm = FakeLLM(_reply())
    monkeypatch.setattr(main, "llm_client", llm)
    with caplog.at_level(logging.INFO):
        state = main._build_state(_payload())

    # Every retrieval node reads get_question_for_processing.
    assert get_question_for_processing(state) == ENGLISH
    # The user's own wording is kept for generation and value linking.
    assert get_original_question(state) == FRENCH
    assert state["path_state"]["question_language"] == "fr"
    assert state["path_state"]["ontology_language"] == "en"
    assert state["path_state"]["question_translated"] == ENGLISH
    assert state["restore_from"]["path_state"]["question_language"] == "fr"
    infos = [r for r in caplog.records if r.getMessage().startswith("Question trans")]
    assert len(infos) == 1

    text = "\n".join(str(c) for _kind, c in _run_generation(monkeypatch, state))
    assert f"Original user request:\n{FRENCH}" in text
    assert f"Normalized retrieval question:\n{ENGLISH}" in text
    assert "own wording" in text
    assert "Write the prose answer (the response text) in French" in text


def test_a_resolved_translation_is_not_translated_twice(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "llm_client", FakeLLM(boom=True))
    payload = _payload()
    payload["question_language"] = ql.QuestionLanguage("fr", "en", ENGLISH, True)
    state = main._build_state(payload)
    assert get_question_for_processing(state) == ENGLISH


def test_translation_failure_keeps_the_original_question(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "llm_client", FakeLLM(boom=True))
    state = main._build_state(_payload())
    assert get_question_for_processing(state) == FRENCH
    assert "question_language" not in state["path_state"]


def test_english_question_costs_no_call_and_is_recorded_as_english(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(boom=True)
    monkeypatch.setattr(main, "llm_client", llm)
    state = main._build_state(_payload("How many orders did we get?"))
    assert not llm.calls
    assert state["path_state"]["question_language"] == "en"
    assert not ql.language_instruction(state["path_state"])


# --- same language / kill switch: unchanged --------------------------------


def test_same_language_as_the_ontology_leaves_state_and_prompts_unchanged(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(_reply())
    monkeypatch.setattr(main, "llm_client", llm)
    question = "How many orders did we get?"
    state = main._build_state(_payload(question))

    # The state holds the submitted question as it did before this feature;
    # an untranslated question only records its language fields.
    assert state["initial_question"] == question
    assert state["path_state"]["processing_question"] == question
    assert not state["path_state"]["question_was_translated"]
    assert state["messages"][1].content == question
    assert get_question_for_processing(state) == question
    assert not llm.calls, "the model must not be called when the languages match"

    messages_before = list(state["messages"])
    actual_prompt = _run_generation(monkeypatch, state)

    # The same call on a state hand-built as it was before the feature existed:
    # the prompts must be identical, message for message.
    legacy: dict[str, Any] = {
        "llm": None,
        "initial_question": question,
        "evidence": "",
        "messages": messages_before,
        "connectors": state["connectors"],
        "path_state": {"processing_question": question, "target_db": "db"},
    }
    assert _run_generation(monkeypatch, legacy) == actual_prompt
    assert not any("Write the prose answer" in str(c) for _k, c in actual_prompt)


def test_a_french_question_on_a_french_ontology_is_not_translated(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model reports the question as French and the ontology is French.
    llm = FakeLLM(_reply("fr", FRENCH))
    monkeypatch.setattr(main, "llm_client", llm)
    payload = _payload()
    payload["ontology_language"] = "fr"
    state = main._build_state(payload)
    assert len(llm.calls) == 1  # detection only; the heuristic covers English
    assert get_question_for_processing(state) == FRENCH
    assert not state["path_state"]["question_was_translated"]


def test_an_english_question_on_a_french_ontology_is_translated_to_french(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(_reply("en", FRENCH))
    monkeypatch.setattr(main, "llm_client", llm)
    payload = _payload("How many orders did we receive in March?")
    payload["ontology_language"] = "fr"
    state = main._build_state(payload)
    assert get_question_for_processing(state) == FRENCH
    assert state["path_state"]["ontology_language"] == "fr"
    assert "into French (fr)" in llm.calls[0][0].content
    text = "\n".join(str(c) for _k, c in _run_generation(monkeypatch, state))
    assert "Write the prose answer (the response text) in English" in text


@pytest.mark.parametrize("value", ["true", "1", "on"])
def test_kill_switch_disables_translation(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ql.DISABLED_ENV, value)
    llm = FakeLLM(_reply())
    monkeypatch.setattr(main, "llm_client", llm)
    state = main._build_state(_payload())
    assert get_question_for_processing(state) == FRENCH
    assert state["path_state"] == {"processing_question": FRENCH, "target_db": "db"}
    assert not llm.calls


def test_no_instruction_or_fields_without_a_translation() -> None:
    assert ql.language_instruction({}) == ""
    assert ql.language_instruction(None) == ""
    assert ql.answer_fields({}) == {}
    assert ql.answer_fields(None) == {}


# --- answer fields ----------------------------------------------------------


def _stream(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch, path_state: dict[str, Any]
) -> dict[str, Any]:
    final = {
        "format_response": {
            "path_state": {
                **path_state,
                "final_response": {"response": "ok", "sql_code": "SELECT 1"},
            }
        }
    }

    def items() -> Iterator[tuple[str, Any]]:
        yield ("updates", final)

    monkeypatch.setattr(main, "_build_state", lambda payload: {"path_state": {}})
    monkeypatch.setattr(
        main,
        "app",
        SimpleNamespace(stream=lambda _s, stream_mode=None, config=None: items()),
    )
    events = list(main.stream_agent_response(cast(TextToSQLPayload, {"question": "q"})))
    (result,) = [e for e in events if e["type"] == "result"]
    return result["answer"]


def test_answer_carries_language_and_english(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = _stream(
        main,
        monkeypatch,
        {
            "question_language": "fr",
            "question_translated": ENGLISH,
            "ontology_language": "en",
        },
    )
    assert answer["question_language"] == "fr"
    assert answer["question_translated"] == ENGLISH
    assert answer["ontology_language"] == "en"


def test_answer_has_no_language_fields_when_translation_did_not_run(
    main: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = _stream(main, monkeypatch, {})
    assert "question_language" not in answer
    assert "question_translated" not in answer
    assert "ontology_language" not in answer
