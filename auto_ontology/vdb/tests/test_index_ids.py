# SPDX-License-Identifier: Apache-2.0

"""Reading and replacing a collection's rows by the ``id`` in their metadata.

Both run SQL against the JSONB metadata, so they are exercised against a real
collection table: the id is not a column, and a filter that silently matched
nothing would make the index look empty and a rebuild duplicate every row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from auto_ontology.infra.postgres import get_postgres_connection_string
from auto_ontology.vdb.postgres import PostgresVDB


@pytest.fixture
def vdb() -> Iterator[PostgresVDB]:
    """A store over a throwaway collection shaped like the real one."""
    name = f"probe_{uuid.uuid4().hex[:8]}"
    try:
        conn = psycopg.connect(get_postgres_connection_string(), connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 -- no database, nothing to assert
        pytest.skip(f"postgres not reachable: {exc}")

    with conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS vdb")
            cur.execute(
                f"CREATE TABLE vdb.{name} ("
                "  langchain_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                "  content text,"
                "  database_name varchar(100),"
                "  label varchar(100),"
                "  langchain_metadata jsonb)"
            )
        conn.commit()
    try:
        yield PostgresVDB(
            connection_string=get_postgres_connection_string(),
            collection_name=name,
            schema_name="vdb",
        )
    finally:
        with psycopg.connect(get_postgres_connection_string()) as cleanup:
            with cleanup.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS vdb.{name}")
            cleanup.commit()


def _insert(vdb: PostgresVDB, database_name: str, label: str, node_id: str) -> None:
    with psycopg.connect(vdb.connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO vdb.{vdb.collection_name} "
                "(content, database_name, label, langchain_metadata) "
                "VALUES (%s, %s, %s, %s)",
                (node_id, database_name, label, json.dumps({"id": node_id})),
            )
        conn.commit()


def test_ids_are_scoped_to_the_database_and_the_labels(vdb: PostgresVDB) -> None:
    _insert(vdb, "pagila", "Term", "t1")
    _insert(vdb, "pagila", "ColumnAttribute", "a1")
    _insert(vdb, "pagila", "SqlAttribute", "s1")
    _insert(vdb, "chinook", "Term", "t2")

    assert vdb.ids_by_database("pagila", ["Term", "ColumnAttribute"]) == {"t1", "a1"}


def test_duplicate_rows_count_once(vdb: PostgresVDB) -> None:
    _insert(vdb, "pagila", "Term", "t1")
    _insert(vdb, "pagila", "Term", "t1")

    assert vdb.ids_by_database("pagila", ["Term"]) == {"t1"}


def test_delete_by_ids_leaves_other_databases_and_other_ids(vdb: PostgresVDB) -> None:
    """A Term represented in two databases has a row in each under one id."""
    _insert(vdb, "pagila", "Term", "shared")
    _insert(vdb, "pagila", "Term", "kept")
    _insert(vdb, "chinook", "Term", "shared")

    assert vdb.delete_by_ids(["shared", "absent"], "pagila") == 1

    assert vdb.ids_by_database("pagila", ["Term"]) == {"kept"}
    assert vdb.ids_by_database("chinook", ["Term"]) == {"shared"}


def test_a_collection_that_does_not_exist_yet_is_empty() -> None:
    try:
        psycopg.connect(get_postgres_connection_string(), connect_timeout=3).close()
    except Exception as exc:  # noqa: BLE001 -- no database, nothing to assert
        pytest.skip(f"postgres not reachable: {exc}")
    missing = PostgresVDB(
        connection_string=get_postgres_connection_string(),
        collection_name=f"absent_{uuid.uuid4().hex[:8]}",
        schema_name="vdb",
    )

    assert missing.ids_by_database("pagila", ["Term"]) == set()
    assert missing.delete_by_ids(["t1"], "pagila") == 0
