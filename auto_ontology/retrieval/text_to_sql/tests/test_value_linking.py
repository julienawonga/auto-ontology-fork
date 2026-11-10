# SPDX-License-Identifier: Apache-2.0

"""Looking a question's phrases up in the value index."""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql import value_linking
from auto_ontology.retrieval.text_to_sql.prompts import format_value_anchors_section
from auto_ontology.retrieval.text_to_sql.value_anchors import (
    split_schema_named_anchors,
)
from auto_ontology.retrieval.text_to_sql.value_linking import (
    candidate_phrases,
    link_question_values,
    normalize_value,
)
from auto_ontology.semantic.constants import LABEL_COLUMN_VALUE


def _row(value: str, table: str, column: str) -> dict[str, Any]:
    return {
        "value": value,
        "schema_name": "public",
        "table_name": table,
        "column_name": column,
    }


_ROWS = [
    _row("United States", "country", "country"),
    _row("India", "country", "country"),
    _row("Peru", "country", "country"),
    _row("Comedy", "category", "name"),
    _row("Drama", "category", "name"),
    _row("PG-13", "film", "rating"),
    _row("Zürich", "city", "city"),
]


class _Vdb:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, str]] = []

    def values_by_database(self, database_name: str, label: str) -> list[dict]:
        self.reads.append((database_name, label))
        return self.rows.get(database_name, [])


class _Retriever:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.vdb = _Vdb(rows)


class _Search:
    """A stand-in for ``search_semantic_index`` that answers from a table."""

    def __init__(self, hits: dict[str, list[tuple[str, str, str, float]]]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def __call__(self, retriever: Any, entity: str, **kwargs: Any) -> list[dict]:
        self.calls.append({"entity": entity, **kwargs})
        return [
            {
                "text": value,
                "id": f"{table}.{column}.{value}",
                "label": LABEL_COLUMN_VALUE,
                "score": distance,
                "name": value,
                "schema_name": "public",
                "database_name": kwargs.get("database_name"),
                "table_name": table,
                "source_column": column,
            }
            for value, table, column, distance in self.hits.get(entity, [])
        ]


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    value_linking.clear_value_cache()


@pytest.fixture
def retriever() -> _Retriever:
    return _Retriever({"pagila": list(_ROWS)})


def _link(
    question: str,
    retriever: Any,
    search: Any = None,
    *,
    entities: list[str] | None = None,
    database: str | None = "pagila",
    **kwargs: Any,
) -> list[dict[str, str]]:
    return link_question_values(
        question,
        entities or [],
        database,
        retriever,
        search=search or _Search({}),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Normalisation and phrases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PG-13", "pg 13"),
        ("pg 13", "pg 13"),
        ("  United   States ", "united states"),
        ("Zürich", "zurich"),
        ("Saint-Étienne!", "saint etienne"),
    ],
)
def test_normalisation_folds_case_accents_and_punctuation(
    text: str, expected: str
) -> None:
    assert normalize_value(text) == expected


def test_phrases_drop_stopword_edges_and_bare_numbers() -> None:
    texts = [
        p.text for p in candidate_phrases("How many customers are from the USA?", [])
    ]

    assert "USA" in texts
    assert "customers" in texts
    assert "How many" not in texts
    assert "from the USA" not in texts
    assert "the" not in texts
    assert "customers are" not in texts


def test_bare_numbers_are_not_phrases() -> None:
    texts = [p.text for p in candidate_phrases("films longer than 120 minutes", [])]

    assert "120" not in texts
    assert "minutes" in texts


def test_entities_come_first() -> None:
    phrases = candidate_phrases("Indai customers", ["Indai", "customers"])

    assert [p.text for p in phrases[:2]] == ["Indai", "customers"]
    assert all(p.from_entity for p in phrases[:2])


# --------------------------------------------------------------------------
# The exact path
# --------------------------------------------------------------------------


def test_a_value_typed_in_another_case_matches_without_being_searched(
    retriever: _Retriever,
) -> None:
    search = _Search({})

    anchors = _link("How many films are rated pg-13?", retriever, search)

    assert anchors == [
        {
            "phrase": "pg-13",
            "kind": "value",
            "tbl": "film",
            "col": "rating",
            "stored_value": "PG-13",
        }
    ]
    # Other phrases of the question may still be looked up; this one never is.
    assert "pg-13" not in [call["entity"] for call in search.calls]


def test_punctuation_and_a_plural_are_forgiven(retriever: _Retriever) -> None:
    by_phrase = {
        a["phrase"]: a["stored_value"]
        for a in _link("Which comedies are rated pg 13?", retriever)
    }

    assert by_phrase == {"comedies": "Comedy", "pg 13": "PG-13"}


def test_accents_are_forgiven(retriever: _Retriever) -> None:
    (anchor,) = _link("customers living in Zurich", retriever)

    assert anchor["stored_value"] == "Zürich"


def test_a_longer_phrase_is_taken_whole(retriever: _Retriever) -> None:
    """ "United States" is one anchor, not that plus "states" on its own."""
    retriever.vdb.rows["pagila"].append(_row("States", "region", "name"))

    anchors = _link("customers in United States", retriever)

    assert [a["stored_value"] for a in anchors] == ["United States"]


def test_two_values_in_one_question_give_two_anchors(retriever: _Retriever) -> None:
    anchors = _link("How many Drama films are rated PG-13?", retriever)

    assert {(a["tbl"], a["stored_value"]) for a in anchors} == {
        ("category", "Drama"),
        ("film", "PG-13"),
    }


def test_a_value_stored_in_two_columns_is_reported_for_both(
    retriever: _Retriever,
) -> None:
    retriever.vdb.rows["pagila"].append(_row("Peru", "city", "country_name"))

    anchors = _link("customers in Peru", retriever)

    assert {(a["tbl"], a["col"]) for a in anchors} == {
        ("country", "country"),
        ("city", "country_name"),
    }


def test_anchors_are_capped_per_question(
    retriever: _Retriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VALUE_LINK_MAX_ANCHORS", "2")

    anchors = _link("Drama Comedy India Peru films", retriever)

    assert len(anchors) == 2


# --------------------------------------------------------------------------
# The fuzzy path
# --------------------------------------------------------------------------


def test_a_typo_is_matched_through_the_search_and_the_lexical_ratio(
    retriever: _Retriever,
) -> None:
    # The embedding is far (distance 0.5 -> similarity 0.5): the characters
    # alone carry "Indai" to "India".
    search = _Search(
        {
            "Indai": [
                ("India", "country", "country", 0.5),
                ("Peru", "country", "country", 0.6),
            ]
        }
    )

    anchors = _link(
        "How many customers live in Indai?", retriever, search, entities=["Indai"]
    )

    assert anchors == [
        {
            "phrase": "Indai",
            "kind": "value",
            "tbl": "country",
            "col": "country",
            "stored_value": "India",
        }
    ]
    assert search.calls[0]["label_filter"] == [LABEL_COLUMN_VALUE]
    assert search.calls[0]["database_name"] == "pagila"


def test_an_abbreviation_is_matched_on_embedding_similarity(
    retriever: _Retriever,
) -> None:
    """ "Bharat" shares no characters with "India"; only the embedding can."""
    search = _Search({"Bharat": [("India", "country", "country", 0.12)]})

    anchors = _link(
        "How many customers are from Bharat?", retriever, search, entities=["Bharat"]
    )

    assert [(a["phrase"], a["stored_value"]) for a in anchors] == [("Bharat", "India")]


def test_a_value_that_does_not_exist_gets_no_anchor(retriever: _Retriever) -> None:
    """Close enough neither in meaning nor in spelling: nothing to "correct" to."""
    search = _Search(
        {
            "Atlantis": [
                ("India", "country", "country", 0.45),
                ("India", "country", "country", 0.5),
            ]
        }
    )

    assert (
        _link("customers from Atlantis", retriever, search, entities=["Atlantis"]) == []
    )


def test_the_thresholds_are_knobs(
    retriever: _Retriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = _Search({"Bharat": [("India", "country", "country", 0.3)]})

    assert _link("customers from Bharat", retriever, search, entities=["Bharat"]) == []

    monkeypatch.setenv("VALUE_LINK_MIN_SIMILARITY", "0.65")
    assert (
        len(_link("customers from Bharat", retriever, search, entities=["Bharat"])) == 1
    )


def test_a_second_column_is_reported_only_when_nearly_as_good(
    retriever: _Retriever,
) -> None:
    search = _Search(
        {
            "Bharat": [
                ("India", "country", "country", 0.10),
                ("India", "address", "country_name", 0.12),
                ("India", "store", "region", 0.19),
            ]
        }
    )

    anchors = _link("customers from Bharat", retriever, search, entities=["Bharat"])

    assert [(a["tbl"], a["col"]) for a in anchors] == [
        ("country", "country"),
        ("address", "country_name"),
    ]


def test_hits_from_another_database_are_never_used(retriever: _Retriever) -> None:
    class _Leaky(_Search):
        def __call__(self, retriever: Any, entity: str, **kwargs: Any) -> list[dict]:
            return [
                {**hit, "database_name": "other"}
                for hit in super().__call__(retriever, entity, **kwargs)
            ]

    search = _Leaky({"Bharat": [("India", "country", "country", 0.1)]})

    assert _link("customers from Bharat", retriever, search, entities=["Bharat"]) == []


def test_the_embedding_calls_are_capped(
    retriever: _Retriever, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VALUE_LINK_MAX_FUZZY_PHRASES", "2")
    search = _Search({})

    _link("Quick brown foxes jump over lazy dogs near Springfield", retriever, search)

    assert len(search.calls) == 2


def test_a_phrase_naming_the_schema_is_not_looked_up(retriever: _Retriever) -> None:
    search = _Search({})
    tables = [{"name": "customer", "columns": [{"name": "country"}]}]

    _link("customer", retriever, search, relevant_tables=tables)

    assert search.calls == []


def test_a_failing_embedding_endpoint_keeps_the_exact_matches(
    retriever: _Retriever,
) -> None:
    def _down(*_a: Any, **_kw: Any) -> list[dict]:
        raise RuntimeError("502 Bad Gateway")

    anchors = _link("Drama films from Bharat", retriever, _down, entities=["Bharat"])

    assert [a["stored_value"] for a in anchors] == ["Drama"]


# --------------------------------------------------------------------------
# Never in the way
# --------------------------------------------------------------------------


def test_a_database_without_an_index_is_a_no_op_with_no_embedding_call() -> None:
    retriever = _Retriever({})
    search = _Search({"Bharat": [("India", "country", "country", 0.1)]})

    assert _link("customers from Bharat", retriever, search, entities=["Bharat"]) == []
    assert search.calls == []


def test_another_databases_index_is_not_used(retriever: _Retriever) -> None:
    search = _Search({})

    assert _link("Drama films", retriever, search, database="chinook") == []
    assert retriever.vdb.reads == [("chinook", LABEL_COLUMN_VALUE)]
    assert search.calls == []


def test_without_a_database_or_retriever_nothing_is_done(retriever: _Retriever) -> None:
    assert _link("Drama films", retriever, database=None) == []
    assert _link("Drama films", None) == []


def test_any_failure_yields_no_anchors_instead_of_an_error() -> None:
    class _Broken:
        @property
        def vdb(self) -> Any:
            raise RuntimeError("connection refused")

    assert _link("Drama films", _Broken()) == []


def test_the_values_are_read_once_per_database_until_cleared(
    retriever: _Retriever,
) -> None:
    _link("Drama films", retriever)
    _link("Comedy films", retriever)

    assert len(retriever.vdb.reads) == 1

    value_linking.clear_value_cache("pagila")
    _link("Drama films", retriever)

    assert len(retriever.vdb.reads) == 2


def test_an_empty_index_is_cached_too(monkeypatch: pytest.MonkeyPatch) -> None:
    retriever = _Retriever({})

    _link("Drama films", retriever)
    _link("Drama films", retriever)

    assert len(retriever.vdb.reads) == 1


# --------------------------------------------------------------------------
# The shape the rest of the pipeline expects
# --------------------------------------------------------------------------


def test_anchors_have_exactly_the_keys_the_consumers_read(
    retriever: _Retriever,
) -> None:
    anchors = _link("Drama films", retriever)

    assert anchors
    for anchor in anchors:
        assert set(anchor) == {"phrase", "kind", "tbl", "col", "stored_value"}
        assert all(isinstance(v, str) and v for v in anchor.values())


def test_anchors_render_in_the_prompt_section(retriever: _Retriever) -> None:
    section = format_value_anchors_section(_link("Drama films", retriever))

    assert '- "Drama": category."name" = \'Drama\'' in section


def test_anchors_survive_the_schema_split_when_they_name_data(
    retriever: _Retriever,
) -> None:
    anchors = _link("Drama films", retriever)
    tables = [{"name": "film", "columns": [{"name": "title"}]}]

    kept, naming_schema = split_schema_named_anchors(anchors, tables)

    assert kept == anchors
    assert naming_schema == []
