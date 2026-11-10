# SPDX-License-Identifier: Apache-2.0

"""The built-in alias table, and how the lookup uses it."""

from __future__ import annotations

import re
from typing import Any

import pytest

from auto_ontology.retrieval.text_to_sql import value_aliases, value_linking
from auto_ontology.retrieval.text_to_sql.value_aliases import builtin_candidates
from auto_ontology.retrieval.text_to_sql.value_linking import (
    learn_value_alias,
    link_question_values,
)
from auto_ontology.semantic.constants import LABEL_COLUMN_VALUE

# The officially assigned ISO 3166-1 alpha-2 codes, written out here rather than
# derived from the table, so the table is checked against an independent list.
_ISO_3166_ALPHA2 = """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM
BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX
CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG
GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR
IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE
NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF
TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF
WS YE YT ZA ZM ZW
""".split()

_US_POSTAL = """
AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split()


def test_the_iso_list_used_here_is_complete() -> None:
    assert len(_ISO_3166_ALPHA2) == 249
    assert len(set(_ISO_3166_ALPHA2)) == 249


def test_every_iso_country_code_is_covered() -> None:
    """Not a hand-picked few: every officially assigned alpha-2 code resolves."""
    entities = value_aliases._COUNTRIES.strip().splitlines()
    codes = [line.split("|")[0] for line in entities]

    assert sorted(codes) == sorted(_ISO_3166_ALPHA2)
    for code in _ISO_3166_ALPHA2:
        assert builtin_candidates(code), code


def test_every_country_has_an_alpha3_code_and_a_name() -> None:
    for line in value_aliases._COUNTRIES.strip().splitlines():
        alpha2, alpha3, *names = line.split("|")
        assert re.fullmatch(r"[A-Z]{3}", alpha3), line
        assert names, line
        assert builtin_candidates(alpha3), line


def test_alpha3_codes_are_unique() -> None:
    alpha3 = [
        line.split("|")[1] for line in value_aliases._COUNTRIES.strip().splitlines()
    ]

    assert len(alpha3) == len(set(alpha3))


def test_every_us_state_postal_code_is_covered() -> None:
    for code in _US_POSTAL:
        assert builtin_candidates(code), code


@pytest.mark.parametrize("alias", ["us", "USA", "U.S.", "U.S.A.", "America"])
def test_aliases_are_normalised_like_phrases(alias: str) -> None:
    assert any("United States" in names for names in builtin_candidates(alias))


def test_an_unknown_alias_proposes_nothing() -> None:
    assert builtin_candidates("zzzzz") == []


def test_an_ambiguous_alias_names_every_entity() -> None:
    names = {n for entity in builtin_candidates("CA") for n in entity}

    assert {"Canada", "California"} <= names
    assert len(builtin_candidates("CA")) == 2


# --------------------------------------------------------------------------
# In the lookup
# --------------------------------------------------------------------------


def _row(value: str, table: str, column: str, aliases: list[str] | None = None) -> dict:
    return {
        "value": value,
        "schema_name": None,
        "table_name": table,
        "column_name": column,
        "aliases": aliases or [],
    }


class _Vdb:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.added: list[dict[str, Any]] = []

    def values_by_database(self, database_name: str, label: str) -> list[dict]:
        return self.rows

    def add_value_alias(self, database_name: str, **kwargs: Any) -> bool:
        self.added.append({"database_name": database_name, **kwargs})
        return True


class _Retriever:
    def __init__(self, rows: list[dict]) -> None:
        self.vdb = _Vdb(rows)


class _Search:
    def __init__(
        self, hits: list[tuple[str, float]] | None = None, only: str | None = None
    ) -> None:
        self.hits = hits or []
        self.only = only
        self.searched: list[str] = []

    def __call__(self, retriever: Any, entity: str, **kwargs: Any) -> list[dict]:
        self.searched.append(entity)
        if self.only is not None and entity != self.only:
            return []
        return [
            {
                "text": value,
                "id": value,
                "label": LABEL_COLUMN_VALUE,
                "score": distance,
                "name": value,
                "database_name": kwargs.get("database_name"),
                "table_name": "country",
                "source_column": "country",
            }
            for value, distance in self.hits
        ]


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    value_linking.clear_value_cache()


def _link(question: str, rows: list[dict], search: Any = None) -> list[dict]:
    return link_question_values(
        question,
        [],
        "app",
        _Retriever(rows),
        search=search or _Search(),
    )


_COUNTRIES = [
    _row("United States", "country", "country"),
    _row("India", "country", "country"),
]


@pytest.mark.parametrize(
    "question",
    [
        "customers from the us",
        "customers from the USA",
        "customers from the U.S.",
        "customers from America",
        "customers from the U.S.A.",
    ],
)
def test_the_usual_names_of_the_united_states_reach_the_stored_value(
    question: str,
) -> None:
    anchors = _link(question, _COUNTRIES)

    assert [(a["tbl"], a["col"], a["stored_value"]) for a in anchors] == [
        ("country", "country", "United States")
    ]


def test_an_aliased_phrase_is_not_sent_to_the_embedding_endpoint() -> None:
    search = _Search()

    _link("customers from the USA", _COUNTRIES, search)

    assert "USA" not in search.searched


@pytest.mark.parametrize(
    "question",
    [
        "customers from the us",
        "customers from the USA",
        "customers from the U.S.",
        "customers from America",
    ],
)
def test_no_anchor_when_the_database_has_no_such_value(question: str) -> None:
    """The alias proposes; it never invents a value the database lacks."""
    rows = [_row("India", "country", "country"), _row("Peru", "country", "country")]

    assert _link(question, rows) == []


def test_a_value_stored_under_another_spelling_is_found_too() -> None:
    rows = [_row("United States of America", "country", "name")]

    (anchor,) = _link("customers from the US", rows)

    assert anchor["stored_value"] == "United States of America"


def test_a_stored_abbreviation_is_matched_as_itself() -> None:
    """Reverse direction: the data stores "USA" and the user types the long name."""
    rows = [_row("USA", "customer", "country")]

    (anchor,) = _link("customers from United States", rows)

    assert anchor["stored_value"] == "USA"


def test_an_ambiguous_alias_gives_every_stored_reading() -> None:
    rows = [
        _row("Canada", "country", "country"),
        _row("California", "state", "name"),
    ]

    anchors = _link("customers in CA", rows)

    assert {a["stored_value"] for a in anchors} == {"Canada", "California"}


def test_an_ambiguous_alias_with_one_stored_reading_has_only_that_one() -> None:
    """Here the data cannot mean the other reading, so nothing is chosen."""
    anchors = _link("customers in CA", [_row("Canada", "country", "country")])

    assert [a["stored_value"] for a in anchors] == ["Canada"]


def test_an_ambiguous_alias_with_no_stored_reading_gives_none() -> None:
    assert _link("customers in CA", [_row("Peru", "country", "country")]) == []


def test_a_value_stored_exactly_is_not_replaced_by_an_alias() -> None:
    rows = [_row("CA", "state", "code"), _row("Canada", "country", "country")]

    anchors = _link("customers in CA", rows)

    assert [a["stored_value"] for a in anchors] == ["CA"]


def test_a_learned_alias_wins_over_the_built_in_one() -> None:
    rows = [
        _row("United States", "country", "country"),
        _row("Etats-Unis", "country", "country", aliases=["us"]),
    ]

    anchors = _link("customers from the US", rows)

    assert [a["stored_value"] for a in anchors] == ["Etats-Unis"]


def test_precedence_learned_then_built_in_then_embedding() -> None:
    search = _Search([("India", 0.05)], only="Hindustan")
    rows = [
        _row("United States", "country", "country"),
        _row("India", "country", "country"),
    ]

    # Built-in beats the embedding, which would have said India.
    built_in = _link("customers from the USA", rows, search)
    assert [a["stored_value"] for a in built_in] == ["United States"]

    # With no alias to use, the embedding path is what answers.
    value_linking.clear_value_cache()
    unknown = link_question_values(
        "customers from Hindustan",
        ["Hindustan"],
        "app",
        _Retriever(rows),
        search=search,
    )
    assert [a["stored_value"] for a in unknown] == ["India"]


def test_a_learned_alias_is_scoped_to_its_own_column() -> None:
    rows = [
        _row("Etats-Unis", "country", "country", aliases=["usa"]),
        _row("Other", "region", "name"),
    ]

    (anchor,) = _link("customers from the USA", rows)

    assert (anchor["tbl"], anchor["col"]) == ("country", "country")


def test_learning_an_alias_stores_it_with_the_value_and_drops_the_cache() -> None:
    retriever = _Retriever(_COUNTRIES)
    link_question_values("x", [], "app", retriever, search=_Search())

    assert learn_value_alias(
        retriever,
        "app",
        schema_name=None,
        table="country",
        column="country",
        value="United States",
        alias=" yanks ",
    )

    assert retriever.vdb.added == [
        {
            "database_name": "app",
            "schema_name": None,
            "table_name": "country",
            "column_name": "country",
            "value": "United States",
            "alias": "yanks",
        }
    ]
    retriever.vdb.rows = [
        _row("United States", "country", "country", aliases=["yanks"])
    ]
    anchors = link_question_values(
        "customers from the yanks", [], "app", retriever, search=_Search()
    )
    assert [a["stored_value"] for a in anchors] == ["United States"]


def test_a_blank_alias_is_refused() -> None:
    retriever = _Retriever(_COUNTRIES)

    assert not learn_value_alias(
        retriever,
        "app",
        schema_name=None,
        table="country",
        column="country",
        value="United States",
        alias="  !! ",
    )
    assert retriever.vdb.added == []
