# SPDX-License-Identifier: Apache-2.0

"""Does the semantic vector index hold what the relational ontology says it should?

The two are written separately -- Terms and ColumnAttributes to their tables,
their embeddings to the vector index -- and nothing ties the writes together.
An embed endpoint that rejected every request once left the index empty behind
a complete ontology: compilation kept reporting success, and chat retrieved
nothing and quietly answered from the raw schema instead. This is the
comparison that would have said so.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_ontology.dal.datasources import fetch_databases
from auto_ontology.dal.terms import fetch_semantic_node_ids_for_database
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_TERM
from auto_ontology.vdb import get_semantic_vdb


@dataclass(frozen=True)
class SemanticIndexHealth:
    """How much of one database's ontology is present in the semantic index."""

    database_name: str
    #: Terms + ColumnAttributes the relational store holds for the database.
    expected: int
    #: How many of those have a row in the index.
    indexed: int

    @property
    def missing(self) -> int:
        return self.expected - self.indexed


def check_semantic_index(database_name: str) -> SemanticIndexHealth:
    """Compare *database_name*'s Terms and ColumnAttributes with its index rows.

    Matched by id rather than by count: a count is satisfied by duplicates and
    by rows left behind for nodes that no longer exist, either of which can
    mask a node that was never embedded.
    """
    term_ids, attribute_ids = fetch_semantic_node_ids_for_database(database_name)
    expected = term_ids | attribute_ids
    indexed = get_semantic_vdb().ids_by_database(
        database_name, [LABEL_TERM, LABEL_COLUMN_ATTRIBUTE]
    )
    return SemanticIndexHealth(
        database_name=database_name,
        expected=len(expected),
        indexed=len(expected & indexed),
    )


def check_semantic_indexes() -> list[SemanticIndexHealth]:
    """:func:`check_semantic_index` for every database in the catalog."""
    return [check_semantic_index(db["name"]) for db in fetch_databases()]
