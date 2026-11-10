# SPDX-License-Identifier: Apache-2.0

"""What a hit's row carries, per label."""

from __future__ import annotations

from auto_ontology.retrieval.data_access.semantic_search import (
    _hits_to_semantic_rows,
)


def _hit(label: str) -> dict:
    return {
        "text": "India",
        "_distance": 0.1,
        "metadata": {
            "id": "x1",
            "label": label,
            "name": "India",
            "database_name": "pagila",
            "table_name": "country",
            "source_column": "country",
        },
    }


def test_a_value_row_says_which_table_and_column_holds_it() -> None:
    (row,) = _hits_to_semantic_rows([_hit("ColumnValue")])

    assert row["table_name"] == "country"
    assert row["source_column"] == "country"
    assert row["database_name"] == "pagila"


def test_other_labels_rows_are_unchanged() -> None:
    """Their consumers read these dicts by key, and some compare them whole."""
    (row,) = _hits_to_semantic_rows([_hit("ColumnAttribute")])

    assert "table_name" not in row
    assert "source_column" not in row
