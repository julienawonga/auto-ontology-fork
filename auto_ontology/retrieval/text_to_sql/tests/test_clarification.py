# SPDX-License-Identifier: Apache-2.0

"""The clarification detector: when it asks, when it must not, and what it asks.

The model and the semantic index are replaced by fakes, so these tests pin the
decision rules (the code around the model), not the model's judgement.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql import clarification as module
from auto_ontology.retrieval.text_to_sql.clarification import (
    ClarificationConfig,
    ClarificationProposalModel,
    detect_clarification,
    is_clarification_answer,
    validate_options,
)

QUESTION = "How many VIP customers do we have?"

_COLUMN_HIT = {
    "id": "c1",
    "label": "ColumnAttribute",
    "score": 0.4,
    "text": "payment.amount: the amount paid",
}


def _proposal(**overrides: Any) -> ClarificationProposalModel:
    fields: dict[str, Any] = {
        "reasoning": "VIP is not defined anywhere.",
        "kind": "undefined_business_term",
        "term": "VIP customers",
        "covered_by_definition": False,
        "changes_sql_materially": True,
        "question": "What do you mean by VIP customers?",
        "options": [
            {"label": "Total payments above the average"},
            {"label": "Top 10% by total payments", "description": "Ranked by spend"},
            {"label": "Above a fixed amount"},
        ],
    }
    fields.update(overrides)
    return ClarificationProposalModel(**fields)


class _Fakes:
    """Stands in for the semantic index and the model, and records their use."""

    def __init__(
        self, definitions: list[dict] | None = None, proposal: Any = None
    ) -> None:
        self.definitions = definitions or []
        self.proposal = proposal
        self.searches: list[str] = []
        self.model_calls = 0
        self.prompt_text: str | None = None

    def search(self, retriever: object, entity: str, **kwargs: Any) -> list[dict]:
        label = kwargs["label_filter"][0]
        self.searches.append(label)
        if label == "ColumnAttribute":
            return [_COLUMN_HIT]
        return [h for h in self.definitions if h["label"] == label]

    def invoke(self, llm: object, messages: list, schema: type) -> Any:
        self.model_calls += 1
        self.prompt_text = str(messages[0].content)
        if isinstance(self.proposal, Exception):
            raise self.proposal
        return self.proposal


@pytest.fixture(name="fakes")
def fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> _Fakes:
    fakes = _Fakes(proposal=_proposal())
    monkeypatch.setattr(module, "search_semantic_index", fakes.search)
    monkeypatch.setattr(module, "invoke_with_structured_output", fakes.invoke)
    return fakes


def _detect(**overrides: Any):
    kwargs: dict[str, Any] = {
        "question": QUESTION,
        "llm": object(),
        "semantic_retriever": object(),
        "certified_lookup": lambda _attr_id: None,
    }
    kwargs.update(overrides)
    return detect_clarification(**kwargs)


# -- asks -------------------------------------------------------------------


def test_asks_when_the_term_is_undefined_and_matters(fakes: _Fakes) -> None:
    decision = _detect()

    assert decision.reason == "asked"
    clarification = decision.clarification
    assert clarification is not None
    assert clarification.term == "VIP customers"
    assert clarification.original_question == QUESTION
    assert [o.label for o in clarification.options] == [
        "Total payments above the average",
        "Top 10% by total payments",
        "Above a fixed amount",
    ]
    assert fakes.model_calls == 1


def test_event_shape_is_the_documented_contract(fakes: _Fakes) -> None:
    clarification = _detect().clarification
    assert clarification is not None

    event = clarification.event()

    assert event == {
        "type": "clarification",
        "clarification_id": clarification.clarification_id,
        "term": "VIP customers",
        "question": "What do you mean by VIP customers?",
        "options": [
            {"id": "opt-1", "label": "Total payments above the average"},
            {
                "id": "opt-2",
                "label": "Top 10% by total payments",
                "description": "Ranked by spend",
            },
            {"id": "opt-3", "label": "Above a fixed amount"},
        ],
        "allow_other": True,
    }
    # Server-side only: the answer is merged into it on the next request.
    assert "original_question" not in event


def test_each_clarification_gets_its_own_id(fakes: _Fakes) -> None:
    first, second = _detect().clarification, _detect().clarification
    assert first and second and first.clarification_id != second.clarification_id


# -- does not ask -----------------------------------------------------------


def test_a_verified_definition_means_no_question_and_no_model_call(
    fakes: _Fakes,
) -> None:
    fakes.definitions = [
        {"id": "s1", "label": "SqlAttribute", "score": 0.3, "text": "VIP customer: ..."}
    ]

    decision = _detect(certified_lookup=lambda _attr_id: True)

    assert decision.clarification is None
    assert decision.reason == "skipped_verified_definition"
    assert fakes.model_calls == 0


@pytest.mark.parametrize("certification", [False, None])
def test_an_uncertified_or_unavailable_attribute_asks_model(
    fakes: _Fakes, certification: bool | None
) -> None:
    fakes.definitions = [
        {"id": "s1", "label": "SqlAttribute", "score": 0.3, "text": "VIP customer: ..."}
    ]

    decision = _detect(certified_lookup=lambda _attr_id: certification)

    assert decision.reason == "asked"
    assert fakes.model_calls == 1


def test_a_close_custom_analysis_skips_without_certification_lookup(
    fakes: _Fakes,
) -> None:
    fakes.definitions = [
        {"id": "a1", "label": "CustomAnalysis", "score": 0.3, "text": "VIP analysis"}
    ]

    assert _detect().reason == "skipped_verified_definition"
    assert fakes.model_calls == 0


def test_a_close_term_is_shown_to_model_but_does_not_skip(fakes: _Fakes) -> None:
    fakes.definitions = [
        {
            "id": "t1",
            "label": "Term",
            "score": 0.3,
            "text": "Customer: a person with a customer record",
        }
    ]

    assert _detect().reason == "asked"
    assert fakes.model_calls == 1
    assert "Customer: a person with a customer record" in (fakes.prompt_text or "")


def test_a_distant_definition_does_not_count_as_verified(fakes: _Fakes) -> None:
    fakes.definitions = [
        {"id": "s1", "label": "SqlAttribute", "score": 0.7, "text": "loosely related"}
    ]

    assert _detect().reason == "asked"


def test_the_definition_distance_is_a_knob(fakes: _Fakes) -> None:
    fakes.definitions = [
        {"id": "s1", "label": "CustomAnalysis", "score": 0.7, "text": "loosely related"}
    ]

    decision = _detect(
        config=ClarificationConfig(definition_max_distance=0.8),
        certified_lookup=lambda _attr_id: True,
    )

    assert decision.reason == "skipped_verified_definition"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": "schema_or_value_reference"}, "llm_schema_reference"),
        ({"kind": "fully_specified"}, "llm_fully_specified"),
        ({"covered_by_definition": True}, "llm_covered_by_definition"),
        ({"changes_sql_materially": False}, "llm_not_material"),
    ],
)
def test_the_model_can_decline_for_each_reason(
    fakes: _Fakes, overrides: dict[str, Any], reason: str
) -> None:
    fakes.proposal = _proposal(**overrides)

    decision = _detect()

    assert decision.clarification is None
    assert decision.reason == reason


def test_supplied_evidence_counts_as_already_answered(fakes: _Fakes) -> None:
    decision = _detect(evidence="VIP means more than 15 rentals")

    assert decision.reason == "skipped_evidence_supplied"
    assert fakes.model_calls == 0


def test_a_very_short_question_is_not_judged(fakes: _Fakes) -> None:
    assert _detect(question="hi").reason == "skipped_question_too_short"


def test_without_a_model_or_an_index_it_does_not_ask(fakes: _Fakes) -> None:
    assert _detect(llm=None).reason == "skipped_no_llm"
    assert _detect(semantic_retriever=None).reason == "skipped_no_semantic_index"


# -- failure never blocks ---------------------------------------------------


def test_a_model_error_falls_back_to_answering(fakes: _Fakes) -> None:
    fakes.proposal = RuntimeError("LLM unavailable")

    decision = _detect()

    assert decision.clarification is None
    assert decision.reason == "detector_error"


def test_an_empty_model_reply_falls_back_to_answering(fakes: _Fakes) -> None:
    fakes.proposal = None

    assert _detect().reason == "llm_no_result"


def test_a_failed_search_is_an_error_not_an_absent_definition(
    fakes: _Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken index must not read as "nothing defines VIP", which would ask."""

    def broken(*args: Any, **kwargs: Any) -> list[dict]:
        raise ConnectionError("vector store down")

    monkeypatch.setattr(module, "search_semantic_index", broken)

    decision = _detect()

    assert decision.reason == "detector_error"
    assert fakes.model_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"term": None},
        {"question": None},
        {"options": [{"label": "Only one"}]},
        {"term": "x" * 200},
    ],
)
def test_an_unusable_proposal_is_not_asked(
    fakes: _Fakes, overrides: dict[str, Any]
) -> None:
    fakes.proposal = _proposal(**overrides)

    decision = _detect()

    assert decision.clarification is None
    assert decision.reason == "invalid_proposal"


# -- option validation ------------------------------------------------------


def _options(*labels: str) -> list[Any]:
    return [module._ProposedOption(label=label) for label in labels]


def test_two_to_four_options_are_accepted_and_numbered() -> None:
    result = validate_options(_options("A", "B", "C", "D"))

    assert result is not None
    assert [o.id for o in result] == ["opt-1", "opt-2", "opt-3", "opt-4"]


def test_fewer_than_two_or_more_than_four_is_rejected() -> None:
    assert validate_options(_options("A")) is None
    assert validate_options(_options("A", "B", "C", "D", "E")) is None
    assert validate_options([]) is None


def test_duplicates_are_dropped_ignoring_case_and_spacing() -> None:
    result = validate_options(_options("Top 10%", "top  10%", "Above average"))

    assert result is not None
    assert [o.label for o in result] == ["Top 10%", "Above average"]


def test_duplicates_that_leave_one_option_are_rejected() -> None:
    assert validate_options(_options("Same", "same")) is None


def test_empty_labels_are_dropped() -> None:
    result = validate_options(_options("A", "   ", "B"))

    assert result is not None
    assert [o.label for o in result] == ["A", "B"]


@pytest.mark.parametrize(
    "vague", ["Other", "other.", "Something else", "None of the above", "Autre"]
)
def test_a_vague_last_option_is_never_kept(vague: str) -> None:
    result = validate_options(_options("A", "B", vague))

    assert result is not None
    assert [o.label for o in result] == ["A", "B"]


def test_an_overlong_label_is_rejected_not_truncated() -> None:
    assert validate_options(_options("A", "B" * 500)) is None


# -- answer check -----------------------------------------------------------


def test_free_text_reply_check(monkeypatch: pytest.MonkeyPatch, fakes: _Fakes) -> None:
    clarification = _detect().clarification
    assert clarification is not None
    kwargs = {"clarification": clarification, "reply": "top 10%"}

    class _Verdict:
        is_answer = False

    fakes.proposal = _Verdict()
    assert is_clarification_answer(llm=object(), **kwargs) is False

    _Verdict.is_answer = True
    assert is_clarification_answer(llm=object(), **kwargs) is True

    # Failing open: a plain message is the answer by contract.
    fakes.proposal = None
    assert is_clarification_answer(llm=object(), **kwargs) is True
    fakes.proposal = RuntimeError("down")
    assert is_clarification_answer(llm=object(), **kwargs) is True
    assert is_clarification_answer(llm=None, **kwargs) is True
