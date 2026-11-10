# SPDX-License-Identifier: Apache-2.0

"""Primary- and foreign-key introspection tests for the Postgres connector.

``get_pks`` and ``get_fks`` used to read ``information_schema``, which lost the
keys of partitioned tables, multiplied composite keys and mixed up tables that
reuse a constraint name. Each of those needs a real server to reproduce, so
these tests build their own throwaway schema in the ``postgres`` database and
are skipped when no server is reachable.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import pandas as pd
import pytest

from auto_ontology.connectors.postgres import PostgresDatabase

# ``{s}`` is the throwaway schema. One schema serves every test: each case
# below is also a distractor for the others, which is what "exact rows" needs.
_DDL = """
    CREATE SCHEMA {s};

    CREATE TABLE {s}.customer (customer_id int PRIMARY KEY);

    -- Self-reference: both ends of the key are the same table.
    CREATE TABLE {s}.staff (
        staff_id   int PRIMARY KEY,
        manager_id int REFERENCES {s}.staff
    );

    -- (a) Partitioned table whose keys exist only on its leaf partitions, the
    -- way Pagila declares ``payment``. One leaf sits under a sub-partitioned
    -- child (nested), the other is attached with its columns in another
    -- order, so its attnums differ from the parent's.
    CREATE TABLE {s}.payment (
        payment_id  int  NOT NULL,
        customer_id int  NOT NULL,
        staff_id    int  NOT NULL,
        paid_on     date NOT NULL
    ) PARTITION BY RANGE (paid_on);
    CREATE TABLE {s}.payment_2022 (
        staff_id    int  NOT NULL,
        paid_on     date NOT NULL,
        customer_id int  NOT NULL,
        payment_id  int  NOT NULL
    );
    ALTER TABLE {s}.payment ATTACH PARTITION {s}.payment_2022
        FOR VALUES FROM ('2022-01-01') TO ('2023-01-01');
    CREATE TABLE {s}.payment_2023 PARTITION OF {s}.payment
        FOR VALUES FROM ('2023-01-01') TO ('2024-01-01')
        PARTITION BY RANGE (paid_on);
    CREATE TABLE {s}.payment_2023_h1 PARTITION OF {s}.payment_2023
        FOR VALUES FROM ('2023-01-01') TO ('2023-07-01');
    ALTER TABLE {s}.payment_2022
        ADD PRIMARY KEY (paid_on, payment_id),
        ADD FOREIGN KEY (customer_id) REFERENCES {s}.customer,
        ADD FOREIGN KEY (staff_id) REFERENCES {s}.staff;
    ALTER TABLE {s}.payment_2023_h1
        ADD PRIMARY KEY (paid_on, payment_id),
        ADD FOREIGN KEY (customer_id) REFERENCES {s}.customer,
        ADD FOREIGN KEY (staff_id) REFERENCES {s}.staff;

    -- (b) Composite key. The referenced table is partitioned with its key on
    -- the parent, so Postgres also keeps one copy of the constraint per
    -- referenced partition; those copies must fold into the same two rows.
    CREATE TABLE {s}.invoice (
        region     text NOT NULL,
        invoice_no int  NOT NULL,
        PRIMARY KEY (region, invoice_no)
    ) PARTITION BY LIST (region);
    CREATE TABLE {s}.invoice_eu PARTITION OF {s}.invoice FOR VALUES IN ('eu');
    CREATE TABLE {s}.invoice_us PARTITION OF {s}.invoice FOR VALUES IN ('us');
    CREATE TABLE {s}.invoice_line (
        line_id    int PRIMARY KEY,
        inv_no     int,
        inv_region text,
        FOREIGN KEY (inv_region, inv_no)
            REFERENCES {s}.invoice (region, invoice_no)
    );

    -- (c) Two tables reusing one constraint name, legal because names are
    -- unique per table only.
    CREATE TABLE {s}.order_a (
        id          int PRIMARY KEY,
        customer_id int,
        CONSTRAINT shared_fk FOREIGN KEY (customer_id) REFERENCES {s}.customer
    );
    CREATE TABLE {s}.order_b (
        id       int PRIMARY KEY,
        staff_id int,
        CONSTRAINT shared_fk FOREIGN KEY (staff_id) REFERENCES {s}.staff
    );

    -- (d) Ordinary inheritance. The child is a table in its own right and
    -- keys are not inherited, so each side must keep exactly its own.
    CREATE TABLE {s}.note (
        note_id  int PRIMARY KEY,
        staff_id int REFERENCES {s}.staff
    );
    CREATE TABLE {s}.note_archive (
        archived_on date NOT NULL,
        customer_id int REFERENCES {s}.customer,
        PRIMARY KEY (archived_on, note_id)
    ) INHERITS ({s}.note);
"""


def _live_postgres_or_skip() -> PostgresDatabase:
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("POSTGRES_* not set")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    try:
        return PostgresDatabase(
            f"postgresql://{user}:{password}@{host}:{port}/postgres"
        )
    except Exception as exc:  # noqa: BLE001 — any failure means "no fixture DB"
        pytest.skip(f"postgres not reachable: {exc}")


@pytest.fixture(scope="module")
def keyed_schema() -> Iterator[tuple[PostgresDatabase, str]]:
    """A connector and the name of a schema holding the tables in ``_DDL``."""
    database = _live_postgres_or_skip()
    schema = f"lexis_keys_{uuid.uuid4().hex[:12]}"
    try:
        database.execute(_DDL.format(s=schema))
        yield database, schema
    finally:
        try:
            database.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            database.close()


def _rows(frame: pd.DataFrame, schema: str, columns: list[str]) -> list[tuple]:
    """*columns* of the rows of *frame* that belong to *schema*, in order."""
    own = frame[frame.table_schema == schema]
    return list(own[columns].itertuples(index=False, name=None))


def test_fks_are_exact(keyed_schema: tuple[PostgresDatabase, str]) -> None:
    """One row per column pair, under catalogued names, and nothing else.

    Each expected row is a case the old query got wrong or must keep right:
    ``payment`` had no rows at all (they sat under ``payment_2022`` and
    ``payment_2023_h1``), ``invoice_line`` had four, and ``order_a`` /
    ``order_b`` each had the other's column as well.
    """
    database, schema = keyed_schema

    fks = database.get_fks()

    assert list(fks.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "referenced_schema",
        "referenced_table",
        "referenced_column",
    ]
    assert _rows(
        fks,
        schema,
        ["table_name", "column_name", "referenced_table", "referenced_column"],
    ) == [
        ("invoice_line", "inv_no", "invoice", "invoice_no"),
        ("invoice_line", "inv_region", "invoice", "region"),
        ("note", "staff_id", "staff", "staff_id"),
        ("note_archive", "customer_id", "customer", "customer_id"),
        ("order_a", "customer_id", "customer", "customer_id"),
        ("order_b", "staff_id", "staff", "staff_id"),
        ("payment", "customer_id", "customer", "customer_id"),
        ("payment", "staff_id", "staff", "staff_id"),
        ("staff", "manager_id", "staff", "staff_id"),
    ]
    assert set(fks[fks.table_schema == schema].referenced_schema) == {schema}


def test_pks_are_exact(keyed_schema: tuple[PostgresDatabase, str]) -> None:
    """Key columns in key order, the partitioned parents included.

    ``payment`` declares its key on the leaves only and ``invoice`` on the
    parent (which Postgres copies to every partition); both must come back
    once, under the parent. ``note_archive`` keeps its own key, not ``note``'s.
    """
    database, schema = keyed_schema

    pks = database.get_pks()

    assert list(pks.columns) == [
        "table_schema",
        "table_name",
        "column_name",
        "ordinal_position",
    ]
    assert _rows(pks, schema, ["table_name", "column_name", "ordinal_position"]) == [
        ("customer", "customer_id", 1),
        ("invoice", "region", 1),
        ("invoice", "invoice_no", 2),
        ("invoice_line", "line_id", 1),
        ("note", "note_id", 1),
        ("note_archive", "archived_on", 1),
        ("note_archive", "note_id", 2),
        ("order_a", "id", 1),
        ("order_b", "id", 1),
        ("payment", "paid_on", 1),
        ("payment", "payment_id", 2),
        ("staff", "staff_id", 1),
    ]


def test_keys_only_name_catalogued_tables(
    keyed_schema: tuple[PostgresDatabase, str],
) -> None:
    """The point of the partition mapping: every key lands on a listed table."""
    database, schema = keyed_schema

    tables = database.get_tables()
    catalogued = set(tables[tables.table_schema == schema].table_name)
    fks = database.get_fks()
    fks = fks[fks.table_schema == schema]
    pks = database.get_pks()
    pks = pks[pks.table_schema == schema]

    assert set(fks.table_name) <= catalogued
    assert set(fks.referenced_table) <= catalogued
    assert set(pks.table_name) <= catalogued
