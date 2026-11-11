# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every connector honours ``execute(..., timeout_s=...)``.

Those that advertise ``supports_read_only_execution`` are checked here too for
``read_only=True``: a real refused write on SQLite and DuckDB, the statement
sent on Postgres and MySQL. ClickHouse's are in ``test_clickhouse.py``.

The text-to-SQL agent caps each statement at the SQL Query Timeout from Agent
Settings, but only on connectors advertising ``supports_statement_timeout``.
SQLite and DuckDB run in-process, so they are exercised against a real slow
query; the network engines are checked against a fake driver for the setting
they send, plus a live Postgres check when a fixture database is reachable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import duckdb
import httpx
import mysql.connector
import pandas as pd
import psycopg
import pytest
import snowflake.connector
from pytest import MonkeyPatch

from auto_ontology.connectors import heavydb as heavydb_module
from auto_ontology.connectors.base import StatementTimeout
from auto_ontology.connectors.clickhouse import ClickHouseDatabase, ClickHouseError
from auto_ontology.connectors.connection_string_factory import build_connection_string
from auto_ontology.connectors.databricks import DatabricksDatabase
from auto_ontology.connectors.duckdb import DuckDBDatabase
from auto_ontology.connectors.heavydb import HeavyDBDatabase
from auto_ontology.connectors.mysql import MySQLDatabase
from auto_ontology.connectors.postgres import PostgresDatabase
from auto_ontology.connectors.registry import CONNECTOR_REGISTRY
from auto_ontology.connectors.snowflake import SnowflakeDatabase
from auto_ontology.connectors.sqlite import SQLiteDatabase
from auto_ontology.connectors.trino import TrinoDatabase

# A query that never finishes on its own: SQLite and DuckDB both evaluate the
# recursive CTE until interrupted.
_ENDLESS_SQL = """
    WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n)
    SELECT count(*) FROM n
"""


def test_every_registered_connector_supports_a_timeout() -> None:
    missing = [
        name
        for name, cls in CONNECTOR_REGISTRY.items()
        if getattr(cls, "supports_statement_timeout", False) is not True
    ]
    assert missing == []


def test_only_engines_that_can_refuse_a_write_advertise_read_only_execution() -> None:
    """Advertising it without enforcing it would be a false sense of safety.

    Snowflake, Databricks, Kyuubi and HeavyDB have no per-statement read-only
    mode; they rely on the parse-time guard and their grants.
    """
    advertised = {
        cls.__name__
        for cls in CONNECTOR_REGISTRY.values()
        if getattr(cls, "supports_read_only_execution", False)
    }
    assert advertised == {
        "PostgresDatabase",
        "MySQLDatabase",
        "SQLiteDatabase",
        "DuckDBDatabase",
        "ClickHouseDatabase",
        "TrinoDatabase",
    }


def test_timeout_message_is_not_mistaken_for_a_dead_connection() -> None:
    """A cancelled query should be rewritten, not reported as unreachable."""
    from auto_ontology.connectors.db_errors import is_infrastructure_error

    assert not is_infrastructure_error(str(StatementTimeout(30)), "SELECT 1")


# ----------------------------------------------------------------------
# In-process engines: real slow queries
# ----------------------------------------------------------------------


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Iterator[SQLiteDatabase]:
    path = tmp_path / "t.sqlite"
    sqlite3.connect(path).close()
    database = SQLiteDatabase(str(path))
    yield database
    database.close()


def test_sqlite_statement_is_aborted_at_the_deadline(
    sqlite_db: SQLiteDatabase,
) -> None:
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        sqlite_db.execute(_ENDLESS_SQL, timeout_s=0.2)
    assert time.monotonic() - started < 5


def test_sqlite_handler_does_not_outlive_the_statement(
    sqlite_db: SQLiteDatabase,
) -> None:
    with pytest.raises(StatementTimeout):
        sqlite_db.execute(_ENDLESS_SQL, timeout_s=0.1)
    time.sleep(0.15)

    # Past the old deadline; a leftover handler would abort this immediately.
    result = sqlite_db.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        "WHERE i < 200000) SELECT count(*) AS c FROM n"
    )
    assert result["c"].iloc[0] == 200000


def test_sqlite_fast_query_is_unaffected(sqlite_db: SQLiteDatabase) -> None:
    assert sqlite_db.execute("SELECT 1 AS v", timeout_s=5)["v"].iloc[0] == 1


@pytest.mark.parametrize(
    "write",
    [
        "INSERT INTO keep VALUES (2)",
        "UPDATE keep SET v = 3",
        "DELETE FROM keep",
        "DROP TABLE keep",
        "CREATE TABLE other (v INTEGER)",
    ],
)
def test_sqlite_refuses_a_write_when_read_only(
    sqlite_db: SQLiteDatabase, write: str
) -> None:
    sqlite_db.execute("CREATE TABLE keep (v INTEGER)")
    sqlite_db.execute("INSERT INTO keep VALUES (1)")

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        sqlite_db.execute(write, read_only=True, timeout_s=5)

    assert sqlite_db.execute("SELECT v FROM keep")["v"].tolist() == [1]
    assert sqlite_db.get_tables()["table_name"].tolist() == ["keep"]


def test_sqlite_read_only_does_not_outlive_the_statement(
    sqlite_db: SQLiteDatabase,
) -> None:
    """Ingestion shares the connector: its next call must be free to write."""
    sqlite_db.execute("CREATE TABLE keep (v INTEGER)")

    with pytest.raises(sqlite3.OperationalError):
        sqlite_db.execute("INSERT INTO keep VALUES (1)", read_only=True)
    assert sqlite_db.execute("SELECT 1 AS v", read_only=True)["v"].iloc[0] == 1

    conn = sqlite_db._conn  # noqa: SLF001 - the state under test lives on it
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    # The refused write must not leave the transaction ``sqlite3`` opened for it.
    assert conn.in_transaction is False
    sqlite_db.execute("INSERT INTO keep VALUES (2)")
    assert sqlite_db.execute("SELECT v FROM keep")["v"].tolist() == [2]


def test_sqlite_read_only_is_lifted_after_a_timeout(
    sqlite_db: SQLiteDatabase,
) -> None:
    sqlite_db.execute("CREATE TABLE keep (v INTEGER)")

    with pytest.raises(StatementTimeout):
        sqlite_db.execute(_ENDLESS_SQL, timeout_s=0.1, read_only=True)

    sqlite_db.execute("INSERT INTO keep VALUES (1)")
    assert sqlite_db.execute("SELECT v FROM keep")["v"].tolist() == [1]


@pytest.fixture
def duckdb_db() -> Iterator[DuckDBDatabase]:
    database = DuckDBDatabase(":memory:", read_only=False)
    yield database
    database.close()


def test_duckdb_statement_is_interrupted_at_the_deadline(
    duckdb_db: DuckDBDatabase,
) -> None:
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        duckdb_db.execute(_ENDLESS_SQL, timeout_s=0.2)
    assert time.monotonic() - started < 5


def test_duckdb_connection_is_usable_after_a_timeout(
    duckdb_db: DuckDBDatabase,
) -> None:
    with pytest.raises(StatementTimeout):
        duckdb_db.execute(_ENDLESS_SQL, timeout_s=0.1)
    time.sleep(0.15)

    assert duckdb_db.execute("SELECT 42 AS v", timeout_s=5)["v"].iloc[0] == 42


def test_duckdb_timeout_only_stops_its_own_statement(
    duckdb_db: DuckDBDatabase,
) -> None:
    """Profiling runs tables in parallel on one connector.

    One table's timer firing must not interrupt another table's query.
    """
    outcome: dict[str, Any] = {}

    def _long_but_within_cap() -> None:
        try:
            outcome["frame"] = duckdb_db.execute(
                "SELECT sum(hash(i)) AS s FROM range(300000000) t(i)", timeout_s=60
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcome["error"] = exc

    neighbour = threading.Thread(target=_long_but_within_cap)
    neighbour.start()
    time.sleep(0.1)
    with pytest.raises(StatementTimeout):
        duckdb_db.execute(_ENDLESS_SQL, timeout_s=0.2)
    neighbour.join(30)

    assert "error" not in outcome, outcome.get("error")
    assert len(outcome["frame"]) == 1


@pytest.mark.parametrize(
    "write",
    [
        "INSERT INTO keep VALUES (2)",
        "UPDATE keep SET v = 3",
        "DELETE FROM keep",
        "DROP TABLE keep",
        "CREATE TABLE other (v INTEGER)",
        "CREATE VIEW other AS SELECT 1",
    ],
)
def test_duckdb_refuses_a_write_when_read_only(
    duckdb_db: DuckDBDatabase, write: str
) -> None:
    """Opened writable, so only the read-only transaction stands in the way."""
    duckdb_db.execute("CREATE TABLE keep (v INTEGER)")
    duckdb_db.execute("INSERT INTO keep VALUES (1)")

    with pytest.raises(duckdb.TransactionException, match="read-only"):
        duckdb_db.execute(write, read_only=True, timeout_s=5)

    assert duckdb_db.execute("SELECT v FROM keep")["v"].tolist() == [1]
    assert duckdb_db.get_tables()["table_name"].tolist() == ["keep"]


def test_duckdb_read_only_does_not_outlive_the_statement(
    duckdb_db: DuckDBDatabase,
) -> None:
    """Ingestion shares the connector: its next call must be free to write."""
    duckdb_db.execute("CREATE TABLE keep (v INTEGER)")

    with pytest.raises(duckdb.TransactionException):
        duckdb_db.execute("INSERT INTO keep VALUES (1)", read_only=True)
    assert duckdb_db.execute("SELECT 42 AS v", read_only=True)["v"].iloc[0] == 42

    duckdb_db.execute("INSERT INTO keep VALUES (2)")
    assert duckdb_db.execute("SELECT v FROM keep")["v"].tolist() == [2]


def test_duckdb_read_only_is_lifted_after_a_timeout(
    duckdb_db: DuckDBDatabase,
) -> None:
    duckdb_db.execute("CREATE TABLE keep (v INTEGER)")

    with pytest.raises(StatementTimeout):
        duckdb_db.execute(_ENDLESS_SQL, timeout_s=0.1, read_only=True)

    duckdb_db.execute("INSERT INTO keep VALUES (1)")
    assert duckdb_db.execute("SELECT v FROM keep")["v"].tolist() == [1]


def test_duckdb_read_only_works_on_a_file_opened_read_only(tmp_path: Path) -> None:
    """The default way the connector opens a database file."""
    path = tmp_path / "t.duckdb"
    with duckdb.connect(str(path)) as setup:
        setup.execute("CREATE TABLE keep AS SELECT 1 AS v")
    database = DuckDBDatabase(str(path))
    try:
        frame = database.execute("SELECT v FROM keep", read_only=True)
        with pytest.raises(duckdb.Error):
            database.execute("INSERT INTO keep VALUES (2)", read_only=True)
    finally:
        database.close()

    assert frame["v"].tolist() == [1]


def test_kyuubi_timeouts_are_statement_timeouts() -> None:
    from auto_ontology.connectors.kyuubi import KyuubiQueryTimeout

    error = KyuubiQueryTimeout(30, "Kyuubi statement exceeded 30s")
    assert isinstance(error, StatementTimeout)
    assert error.timeout_s == 30
    assert str(error) == "Kyuubi statement exceeded 30s"


@pytest.mark.parametrize(
    ("elapsed_s", "points_at"),
    [
        (None, "Agent Settings"),
        (300.4, "Agent Settings"),
        # Cancelled at 10s under a 300s cap: the warehouse's own limit fired.
        (10.0, "database or warehouse"),
    ],
)
def test_timeout_message_names_the_limit_that_fired(
    elapsed_s: float | None, points_at: str
) -> None:
    message = str(StatementTimeout(300, elapsed_s=elapsed_s))

    assert points_at in message
    if points_at != "Agent Settings":
        assert "will not help" in message


# ----------------------------------------------------------------------
# Postgres
# ----------------------------------------------------------------------


class _PgConnection:
    def __init__(self) -> None:
        self.log: list[Any] = []
        self.cursor_kwargs: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append("BEGIN")
        yield
        self.log.append("COMMIT")

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((sql, params))

    def cursor(self, **kwargs: Any) -> "_PgCursor":
        self.cursor_kwargs.append(kwargs)
        return _PgCursor(self)


class _PgCursor:
    description = [("v",)]

    def __init__(self, conn: _PgConnection) -> None:
        self._conn = conn

    def __enter__(self) -> "_PgCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.log.append(("query", sql))

    def fetchall(self) -> list[dict[str, int]]:
        return [{"v": 1}]

    def fetchmany(self, size: int) -> list[dict[str, int]]:
        self._conn.log.append(("fetchmany", size))
        return [{"v": i} for i in range(10)][:size]


class _PgPool:
    def __init__(self) -> None:
        self.conn = _PgConnection()

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[_PgConnection]:
        yield self.conn


def _fake_postgres() -> tuple[PostgresDatabase, _PgConnection]:
    database = PostgresDatabase.__new__(PostgresDatabase)
    pool = _PgPool()
    database._pool = pool  # noqa: SLF001 - test double for the pool
    database._pool_lock = threading.Lock()  # noqa: SLF001
    return database, pool.conn


def test_postgres_scopes_the_timeout_to_one_transaction() -> None:
    database, conn = _fake_postgres()

    database.execute("SELECT 1", timeout_s=1.5)

    assert conn.log == [
        "BEGIN",
        ("SELECT set_config('statement_timeout', %s, true)", ("1500ms",)),
        ("query", "SELECT 1"),
        "COMMIT",
    ]


def test_postgres_without_timeout_sends_no_setting() -> None:
    database, conn = _fake_postgres()

    database.execute("SELECT 1")

    assert conn.log == [("query", "SELECT 1")]


def test_postgres_read_only_opens_a_read_only_transaction_first() -> None:
    database, conn = _fake_postgres()

    database.execute("SELECT 1", read_only=True)

    assert conn.log == [
        "BEGIN",
        ("SET TRANSACTION READ ONLY", None),
        ("query", "SELECT 1"),
        "COMMIT",
    ]


def test_postgres_read_only_and_timeout_share_one_transaction() -> None:
    database, conn = _fake_postgres()

    database.execute("SELECT 1", timeout_s=2, read_only=True)

    assert conn.log == [
        "BEGIN",
        ("SET TRANSACTION READ ONLY", None),
        ("SELECT set_config('statement_timeout', %s, true)", ("2000ms",)),
        ("query", "SELECT 1"),
        "COMMIT",
    ]


def test_postgres_reads_one_row_past_the_cap() -> None:
    """``max_rows + 1`` rows, so the caller can tell "exactly N" from "more"."""
    database, conn = _fake_postgres()

    frame = database.execute("SELECT v", max_rows=3)

    assert ("fetchmany", 4) in conn.log
    assert len(frame) == 4


def test_postgres_capped_read_uses_a_server_side_cursor_in_a_transaction() -> None:
    """A client-side cursor would load the whole result before ``fetchmany``.

    Only a named cursor makes the server send just the rows asked for, and a
    named cursor needs a transaction (the pool is in autocommit mode).
    """
    database, conn = _fake_postgres()

    database.execute("SELECT v", max_rows=3)

    assert conn.log[0] == "BEGIN"
    assert conn.cursor_kwargs[-1]["name"].startswith("lexis_")


def test_postgres_without_a_cap_reads_everything() -> None:
    database, conn = _fake_postgres()

    database.execute("SELECT v")

    assert ("fetchmany", 4) not in conn.log
    assert "name" not in conn.cursor_kwargs[-1]


def test_postgres_advertises_row_limit() -> None:
    assert PostgresDatabase.supports_row_limit is True


def test_postgres_advertises_read_only_execution() -> None:
    assert PostgresDatabase.supports_read_only_execution is True


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


def test_live_postgres_refuses_a_write_in_a_read_only_transaction() -> None:
    database = _live_postgres_or_skip()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            database.execute("CREATE TABLE read_only_probe (v int)", read_only=True)
        # The setting must not stick to the pooled connection.
        assert database.execute("SHOW transaction_read_only").iloc[0, 0] == "off"
    finally:
        database.execute("DROP TABLE IF EXISTS read_only_probe")
        database.close()


def test_live_postgres_capped_read_does_not_load_the_whole_result() -> None:
    """Billions of rows would take minutes and gigabytes through a client cursor.

    A cross join is produced row by row, unlike ``generate_series`` in ``FROM``,
    which Postgres materialises in full before returning anything.
    """
    database = _live_postgres_or_skip()
    try:
        started = time.monotonic()
        frame = database.execute(
            "SELECT a.oid FROM pg_class a, pg_class b, pg_class c, pg_class d",
            max_rows=5,
        )
        assert len(frame) == 6  # the cap plus the row that shows it was cut
        assert time.monotonic() - started < 5
    finally:
        database.close()


def test_live_postgres_cancels_and_does_not_leak_the_cap() -> None:
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("POSTGRES_* not set")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    try:
        database = PostgresDatabase(
            f"postgresql://{user}:{password}@{host}:{port}/postgres"
        )
    except Exception as exc:  # noqa: BLE001 — any failure means "no fixture DB"
        pytest.skip(f"postgres not reachable: {exc}")
    try:
        with pytest.raises(StatementTimeout):
            database.execute("SELECT pg_sleep(5)", timeout_s=0.3)
        # The pool holds one idle connection; the cap must not have stuck to it.
        setting = database.execute("SHOW statement_timeout").iloc[0, 0]
        assert setting == "0"
    finally:
        database.close()


# ----------------------------------------------------------------------
# MySQL
# ----------------------------------------------------------------------


class _MySQLCursor:
    """Records statements; rejects SETs of *reject* the way the server does."""

    description = [("v",)]

    def __init__(self, reject: tuple[str, ...]) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.attempted: list[str] = []
        self._reject = reject

    def execute(self, sql: str, params: Any = None) -> None:
        self.attempted.append(sql)
        for variable in self._reject:
            if variable in sql:
                raise mysql.connector.Error(
                    msg=f"Unknown system variable '{variable}'", errno=1193
                )
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, int]]:
        return [{"v": 1}]

    def close(self) -> None:
        return None


def _fake_mysql(
    monkeypatch: MonkeyPatch, reject: tuple[str, ...] = ()
) -> tuple[MySQLDatabase, _MySQLCursor]:
    cursor = _MySQLCursor(reject)

    class _Conn:
        def cursor(self, **kwargs: Any) -> _MySQLCursor:
            return cursor

        def ping(self, **kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "auto_ontology.connectors.mysql.mysql.connector.connect",
        lambda **kwargs: _Conn(),
    )
    return MySQLDatabase("mysql://u:p@db.example.com:3306/beaver"), cursor


def test_mysql_sets_max_execution_time(monkeypatch: MonkeyPatch) -> None:
    database, cursor = _fake_mysql(monkeypatch)

    database.execute("SELECT 1", timeout_s=1.5)

    assert cursor.executed == [
        ("SET SESSION max_execution_time = %s", (1500,)),
        ("SELECT 1", None),
    ]


def test_mariadb_falls_back_to_max_statement_time(monkeypatch: MonkeyPatch) -> None:
    database, cursor = _fake_mysql(monkeypatch, reject=("max_execution_time",))

    database.execute("SELECT 1", timeout_s=30)
    database.execute("SELECT 2", timeout_s=30)

    assert cursor.executed == [
        ("SET SESSION max_statement_time = %s", (30.0,)),
        ("SELECT 1", None),
        ("SET SESSION max_statement_time = %s", (30.0,)),
        ("SELECT 2", None),
    ]
    # Learned once: the second statement does not retry the MySQL spelling.
    assert cursor.attempted.count("SET SESSION max_execution_time = %s") == 1


def test_mysql_without_either_variable_runs_uncapped(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Old MySQL / MariaDB: run as before the cap existed, never fail."""
    database, cursor = _fake_mysql(
        monkeypatch, reject=("max_execution_time", "max_statement_time")
    )

    with caplog.at_level("WARNING"):
        database.execute("SELECT 1", timeout_s=30)
        database.execute("SELECT 2", timeout_s=30)

    assert cursor.executed == [("SELECT 1", None), ("SELECT 2", None)]
    # Probed once, warned once.
    assert len(cursor.attempted) == 4
    assert sum("run uncapped" in r.getMessage() for r in caplog.records) == 1


def test_mysql_other_set_errors_are_not_swallowed(monkeypatch: MonkeyPatch) -> None:
    database, cursor = _fake_mysql(monkeypatch)
    error = mysql.connector.Error(msg="Lost connection", errno=2013)
    monkeypatch.setattr(cursor, "execute", lambda sql, params=None: _raise(error))

    with pytest.raises(mysql.connector.Error):
        database.execute("SELECT 1", timeout_s=30)


def test_mysql_without_timeout_sends_no_setting(monkeypatch: MonkeyPatch) -> None:
    database, cursor = _fake_mysql(monkeypatch)

    database.execute("SELECT 1")

    assert cursor.executed == [("SELECT 1", None)]


def test_mysql_read_only_makes_the_session_read_only_first(
    monkeypatch: MonkeyPatch,
) -> None:
    database, cursor = _fake_mysql(monkeypatch)

    database.execute("SELECT 1", timeout_s=1.5, read_only=True)

    assert cursor.executed == [
        ("SET SESSION TRANSACTION READ ONLY", None),
        ("SET SESSION max_execution_time = %s", (1500,)),
        ("SELECT 1", None),
    ]


def test_mysql_read_only_is_sent_only_when_asked(monkeypatch: MonkeyPatch) -> None:
    """Each call opens its own session, so asking once must not carry over."""
    database, cursor = _fake_mysql(monkeypatch)

    database.execute("SELECT 1", read_only=True)
    database.execute("SELECT 2")

    assert cursor.executed == [
        ("SET SESSION TRANSACTION READ ONLY", None),
        ("SELECT 1", None),
        ("SELECT 2", None),
    ]


def test_mysql_runs_nothing_when_the_session_cannot_be_made_read_only(
    monkeypatch: MonkeyPatch,
) -> None:
    """A server too old for the statement must fail, not run unprotected."""
    database, cursor = _fake_mysql(monkeypatch)
    attempted: list[str] = []
    refused = mysql.connector.Error(msg="You have an error in your SQL", errno=1064)

    def _refuse(sql: str, params: Any = None) -> None:
        attempted.append(sql)
        raise refused

    monkeypatch.setattr(cursor, "execute", _refuse)

    with pytest.raises(mysql.connector.Error):
        database.execute("SELECT 1", read_only=True)
    assert attempted == ["SET SESSION TRANSACTION READ ONLY"]


def test_mysql_advertises_read_only_execution() -> None:
    assert MySQLDatabase.supports_read_only_execution is True


# ----------------------------------------------------------------------
# Snowflake
# ----------------------------------------------------------------------


def _fake_snowflake(monkeypatch: MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    class _Cursor:
        description = [("V",)]

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            return None

        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    def connect(**kwargs: Any) -> _Conn:
        seen.append(kwargs)
        return _Conn()

    monkeypatch.setattr(
        "auto_ontology.connectors.snowflake.snowflake.connector.connect", connect
    )
    return seen


def test_snowflake_sets_statement_timeout_session_parameter(
    monkeypatch: MonkeyPatch,
) -> None:
    seen = _fake_snowflake(monkeypatch)
    database = SnowflakeDatabase("snowflake://u:p@acct?warehouse=WH&database=DB")

    database.execute("SELECT 1", timeout_s=45)
    database.execute("SELECT 1")

    assert seen[0]["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 45}
    # Per-call kwargs: the cap must not stick to the connector.
    assert "session_parameters" not in seen[1]


# ----------------------------------------------------------------------
# Trino
# ----------------------------------------------------------------------


class _TrinoError(Exception):
    """Stands in for ``TrinoQueryError``, which the connector reads by attribute."""

    def __init__(self, error_name: str, message: str) -> None:
        super().__init__(message)
        self.error_name = error_name


class _TrinoCursor:
    """Records each statement with the session properties it was sent under."""

    def __init__(self, conn: "_TrinoConnection") -> None:
        self._conn = conn
        self.description: list[tuple[str, str]] | None = None

    def execute(self, sql: str, parameters: Any = None) -> None:
        properties = dict(self._conn._client_session.properties)
        self._conn.sent.append((sql, properties))
        self._conn.transaction_ids.append(self._conn._client_session.transaction_id)
        error = self._conn.fail(sql, properties)
        if error is not None:
            raise error
        self.description = [("v", "")]

    def fetchall(self) -> list[list[int]]:
        return [[1]]

    def close(self) -> None:
        return None


class _TrinoRequest:
    """Stands in for ``TrinoRequest``: answers ``START TRANSACTION`` with an id."""

    def __init__(self, conn: "_TrinoConnection") -> None:
        self._conn = conn

    def post(self, sql: str) -> SimpleNamespace:
        self._conn.posted.append((sql, dict(self._conn._client_session.properties)))
        return SimpleNamespace(
            ok=True,
            status_code=200,
            headers={"X-Trino-Started-Transaction-Id": "txn-1"},
        )

    def process(self, response: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(next_uri=None)


class _TrinoConnection:
    def __init__(self, fail: Any = lambda sql, properties: None) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []
        self.transaction_ids: list[str] = []
        self.posted: list[tuple[str, dict[str, str]]] = []
        self.fail = fail
        self._client_session = SimpleNamespace(
            properties={"existing": "kept"}, transaction_id="NONE"
        )

    def _create_request(self) -> _TrinoRequest:
        return _TrinoRequest(self)

    def cursor(self) -> _TrinoCursor:
        return _TrinoCursor(self)

    def close(self) -> None:
        return None


def _trino(fake: _TrinoConnection) -> TrinoDatabase:
    database = TrinoDatabase(
        build_connection_string(
            {"type": "trino", "host": "t.example.com", "user": "a", "database": "hive"}
        )
    )
    database._connection = fake  # noqa: SLF001 - test double for the driver
    return database


def test_trino_sends_the_cap_with_that_request_only() -> None:
    fake = _TrinoConnection()
    database = _trino(fake)

    result = database.execute("SELECT 1", timeout_s=30)
    database.execute("SELECT 2")

    assert result["v"].tolist() == [1]
    # No SET/RESET round trips: one request each, the cap on the first only.
    assert fake.sent == [
        ("SELECT 1", {"existing": "kept", "query_max_run_time": "30s"}),
        ("SELECT 2", {"existing": "kept"}),
    ]


def test_trino_restores_session_properties_when_the_query_fails() -> None:
    fake = _TrinoConnection(
        fail=lambda sql, properties: RuntimeError("boom") if sql == "SELECT 1" else None
    )
    database = _trino(fake)

    with pytest.raises(RuntimeError, match="boom"):
        database.execute("SELECT 1", timeout_s=30)

    assert fake._client_session.properties == {"existing": "kept"}


def test_trino_runs_uncapped_where_the_property_is_denied(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Access control refusing the property must not fail every query."""
    denied = _TrinoError(
        "PERMISSION_DENIED",
        "Access Denied: Cannot set system session property query_max_run_time",
    )
    fake = _TrinoConnection(
        fail=lambda sql, properties: (
            denied if "query_max_run_time" in properties else None
        )
    )
    database = _trino(fake)

    with caplog.at_level("WARNING"):
        first = database.execute("SELECT 1", timeout_s=30)
        database.execute("SELECT 2", timeout_s=30)

    assert first["v"].tolist() == [1]
    assert [properties for _, properties in fake.sent] == [
        {"existing": "kept", "query_max_run_time": "30s"},
        {"existing": "kept"},
        # Remembered: no second attempt with the property.
        {"existing": "kept"},
    ]
    assert sum("run uncapped" in r.getMessage() for r in caplog.records) == 1


def test_trino_other_permission_errors_are_not_retried() -> None:
    denied = _TrinoError("PERMISSION_DENIED", "Access Denied: Cannot select from t")
    fake = _TrinoConnection(fail=lambda sql, properties: denied)
    database = _trino(fake)

    with pytest.raises(_TrinoError):
        database.execute("SELECT * FROM t", timeout_s=30)
    assert len(fake.sent) == 1


def test_trino_read_only_runs_the_statement_in_a_read_only_transaction() -> None:
    fake = _TrinoConnection()
    database = _trino(fake)

    result = database.execute("SELECT 1", read_only=True)

    assert result["v"].tolist() == [1]
    # The read-only transaction is started by hand (the client has no option
    # for it), the statement carries its id, then it is rolled back with it.
    assert [sql for sql, _ in fake.posted] == ["START TRANSACTION READ ONLY"]
    assert [sql for sql, _ in fake.sent] == ["SELECT 1", "ROLLBACK"]
    assert fake.transaction_ids == ["txn-1", "txn-1"]
    # And the connection is back in autocommit for whoever comes next.
    assert fake._client_session.transaction_id == "NONE"


def test_trino_sends_no_transaction_unless_read_only() -> None:
    fake = _TrinoConnection()
    database = _trino(fake)

    database.execute("SELECT 1")
    database.execute("SELECT 2", timeout_s=30)

    assert fake.posted == []
    assert [sql for sql, _ in fake.sent] == ["SELECT 1", "SELECT 2"]
    assert fake.transaction_ids == ["NONE", "NONE"]


def test_trino_read_only_does_not_outlive_the_statement() -> None:
    fake = _TrinoConnection(
        fail=lambda sql, properties: (
            _TrinoError("READ_ONLY_VIOLATION", "Cannot execute write")
            if sql.startswith("INSERT")
            else None
        )
    )
    database = _trino(fake)

    with pytest.raises(_TrinoError, match="Cannot execute write"):
        database.execute("INSERT INTO t VALUES (1)", read_only=True)
    database.execute("SELECT 2")

    # The refused write was rolled back, and the next statement is plain.
    assert [sql for sql, _ in fake.sent] == [
        "INSERT INTO t VALUES (1)",
        "ROLLBACK",
        "SELECT 2",
    ]
    assert fake.transaction_ids == ["txn-1", "txn-1", "NONE"]
    assert fake._client_session.transaction_id == "NONE"


def test_trino_read_only_and_timeout_share_the_request() -> None:
    fake = _TrinoConnection()
    database = _trino(fake)

    database.execute("SELECT 1", timeout_s=30, read_only=True)
    database.execute("SELECT 2", read_only=True)

    capped = {"existing": "kept", "query_max_run_time": "30s"}
    assert fake.posted == [
        ("START TRANSACTION READ ONLY", capped),
        ("START TRANSACTION READ ONLY", {"existing": "kept"}),
    ]
    assert fake.sent[0] == ("SELECT 1", capped)
    assert fake._client_session.properties == {"existing": "kept"}


def test_trino_read_only_survives_a_denied_timeout_property() -> None:
    denied = _TrinoError(
        "PERMISSION_DENIED",
        "Access Denied: Cannot set system session property query_max_run_time",
    )
    fake = _TrinoConnection(
        fail=lambda sql, properties: (
            denied if "query_max_run_time" in properties and sql == "SELECT 1" else None
        )
    )
    database = _trino(fake)

    result = database.execute("SELECT 1", timeout_s=30, read_only=True)

    assert result["v"].tolist() == [1]
    # The retry is read-only too: a transaction per attempt, none left open.
    assert [sql for sql, _ in fake.posted] == ["START TRANSACTION READ ONLY"] * 2
    assert fake._client_session.transaction_id == "NONE"


def test_trino_read_only_runs_nothing_when_no_transaction_can_start() -> None:
    fake = _TrinoConnection()
    fake._create_request = lambda: SimpleNamespace(  # type: ignore[method-assign]
        post=lambda sql: SimpleNamespace(ok=False, status_code=503, headers={})
    )
    database = _trino(fake)

    with pytest.raises(RuntimeError, match="read-only transaction"):
        database.execute("INSERT INTO t VALUES (1)", read_only=True)

    assert fake.sent == []
    assert fake._client_session.transaction_id == "NONE"


def test_trino_advertises_read_only_execution() -> None:
    assert TrinoDatabase.supports_read_only_execution is True


def _live_trino_or_skip() -> TrinoDatabase:
    """A Trino with the ``memory`` catalog, from ``TRINO_TEST_URL``.

    e.g. ``trino://probe@localhost:8080/memory?schema=default``
    """
    url = os.environ.get("TRINO_TEST_URL")
    if not url:
        pytest.skip("TRINO_TEST_URL not set")
    database = TrinoDatabase(url)
    try:
        database.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "no fixture server"
        pytest.skip(f"trino not reachable: {exc}")
    return database


def test_live_trino_refuses_writes_and_does_not_leak_the_transaction() -> None:
    database = _live_trino_or_skip()
    try:
        database.execute("DROP TABLE IF EXISTS memory.default.read_only_probe")
        database.execute("CREATE TABLE memory.default.read_only_probe (v int)")
        for write in (
            "INSERT INTO memory.default.read_only_probe VALUES (1)",
            "CREATE TABLE memory.default.read_only_probe2 (v int)",
            "ALTER TABLE memory.default.read_only_probe ADD COLUMN w int",
            "DROP TABLE memory.default.read_only_probe",
        ):
            with pytest.raises(Exception, match="read-only transaction") as refused:
                database.execute(write, read_only=True, timeout_s=30)
            assert getattr(refused.value, "error_name", None) == "READ_ONLY_VIOLATION"
        # Nothing was written, a read in the same mode works, and the next
        # call on the same connector is a normal read-write one.
        frame = database.execute(
            "SELECT * FROM memory.default.read_only_probe", read_only=True
        )
        assert list(frame.columns) == ["v"] and frame.empty
        database.execute("INSERT INTO memory.default.read_only_probe VALUES (1)")
        assert (
            database.execute(
                "SELECT count(*) FROM memory.default.read_only_probe"
            ).iloc[0, 0]
            == 1
        )
    finally:
        database.execute("DROP TABLE IF EXISTS memory.default.read_only_probe")
        database.close()


# ----------------------------------------------------------------------
# HeavyDB
# ----------------------------------------------------------------------


class _HeavyClient:
    def __init__(self) -> None:
        self.interrupts: list[tuple[str, str]] = []

    def interrupt(self, query_session: str, interrupt_session: str) -> None:
        self.interrupts.append((query_session, interrupt_session))


def _fake_heavydb(
    monkeypatch: MonkeyPatch, query_seconds: float
) -> tuple[HeavyDBDatabase, _HeavyClient]:
    client = _HeavyClient()
    sessions = iter(f"s{i}" for i in range(100))
    release = threading.Event()

    class _Cursor:
        description = [("v",)]

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            release.wait(query_seconds)

        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

    class _Conn:
        def __init__(self) -> None:
            self._session = next(sessions)
            self._client = client

        def cursor(self) -> _Cursor:
            return _Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(heavydb_module, "_connect_with_timeout", lambda *args: _Conn())
    database = HeavyDBDatabase("heavydb://admin:pw@h.example.com:6274/heavyai")
    return database, client


def test_heavydb_stops_waiting_and_interrupts_the_query(
    monkeypatch: MonkeyPatch,
) -> None:
    database, client = _fake_heavydb(monkeypatch, query_seconds=5)

    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        database.execute("SELECT 1", timeout_s=0.2)

    assert time.monotonic() - started < 2
    assert client.interrupts == [("s0", "s1")]


def test_heavydb_fast_query_returns_normally(monkeypatch: MonkeyPatch) -> None:
    database, client = _fake_heavydb(monkeypatch, query_seconds=0)

    result = database.execute("SELECT 1", timeout_s=5)

    assert result.equals(pd.DataFrame([(1,)], columns=["v"]))
    assert client.interrupts == []


# ----------------------------------------------------------------------
# Every engine's cancellation surfaces as StatementTimeout
# ----------------------------------------------------------------------
#
# Callers recognise a timeout by type: semantic compilation's sampling circuit
# breaker counts ``TimeoutError`` and no server-reported cancellation message,
# so an engine whose cancellation stayed a driver error would never trip it.


def _raise(error: BaseException) -> Any:
    raise error


def test_sampling_breaker_counts_a_statement_timeout() -> None:
    from auto_ontology.semantic.visit_enter import _SamplingCircuitBreaker

    breaker = _SamplingCircuitBreaker()
    connector = SimpleNamespace(database_name="warehouse")

    assert breaker.record_failure(connector, StatementTimeout(120)) is True


def _raising_postgres(error: BaseException) -> PostgresDatabase:
    database, conn = _fake_postgres()

    class _Cursor(_PgCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            raise error

    conn.cursor = lambda **kwargs: _Cursor(conn)  # type: ignore[method-assign]
    return database


def test_postgres_statement_timeout_becomes_statement_timeout() -> None:
    database = _raising_postgres(
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    )

    with pytest.raises(StatementTimeout) as caught:
        database.execute("SELECT 1", timeout_s=30)
    assert caught.value.timeout_s == 30


def test_postgres_cancellation_without_a_timeout_is_left_alone() -> None:
    """A read-only call with no cap must not relabel a cancellation as a timeout."""
    database = _raising_postgres(
        psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    )

    with pytest.raises(psycopg.errors.QueryCanceled):
        database.execute("SELECT 1", read_only=True)


def test_postgres_other_cancellation_is_left_alone() -> None:
    """pg_cancel_backend() is an operator's decision, not our cap."""
    database = _raising_postgres(
        psycopg.errors.QueryCanceled("canceling statement due to user request")
    )

    with pytest.raises(psycopg.errors.QueryCanceled):
        database.execute("SELECT 1", timeout_s=30)


def _mysql_failing_with(monkeypatch: MonkeyPatch, errno: int) -> MySQLDatabase:
    database, cursor = _fake_mysql(monkeypatch)
    error = mysql.connector.Error(msg="statement failed", errno=errno)
    monkeypatch.setattr(
        cursor,
        "execute",
        lambda sql, params=None: _raise(error) if sql.startswith("SELECT") else None,
    )
    return database


@pytest.mark.parametrize("errno", [3024, 1969])
def test_mysql_and_mariadb_timeouts_become_statement_timeout(
    monkeypatch: MonkeyPatch, errno: int
) -> None:
    database = _mysql_failing_with(monkeypatch, errno)

    with pytest.raises(StatementTimeout):
        database.execute("SELECT 1", timeout_s=30)


def test_mysql_other_errors_are_left_alone(monkeypatch: MonkeyPatch) -> None:
    database = _mysql_failing_with(monkeypatch, 1054)

    with pytest.raises(mysql.connector.Error):
        database.execute("SELECT 1", timeout_s=30)


@pytest.mark.parametrize(("errno", "expected"), [(630, StatementTimeout), (2003, None)])
def test_snowflake_only_its_timeout_becomes_statement_timeout(
    monkeypatch: MonkeyPatch, errno: int, expected: type | None
) -> None:
    database = SnowflakeDatabase("snowflake://u:p@acct?warehouse=WH&database=DB")
    error = snowflake.connector.errors.ProgrammingError(msg="failed", errno=errno)
    monkeypatch.setattr(database, "_execute", lambda *args: _raise(error))

    with pytest.raises(expected or snowflake.connector.errors.ProgrammingError):
        database.execute("SELECT 1", timeout_s=30)


def test_trino_time_limit_becomes_statement_timeout() -> None:
    limit = _TrinoError(
        "EXCEEDED_TIME_LIMIT", "Query exceeded maximum time limit of 30.00s"
    )
    fake = _TrinoConnection(fail=lambda sql, properties: limit)
    database = _trino(fake)

    with pytest.raises(StatementTimeout):
        database.execute("SELECT slow", timeout_s=30)
    assert fake._client_session.properties == {"existing": "kept"}


def test_clickhouse_client_side_read_timeout_is_a_statement_timeout() -> None:
    """The server answered nothing within cap + grace: a slow query, not a dead host."""

    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    database = ClickHouseDatabase("clickhouse://u@ch.example.com:8123/db")
    database._client = httpx.Client(  # noqa: SLF001 - test double for the server
        base_url="http://test", transport=httpx.MockTransport(_slow)
    )

    with pytest.raises(StatementTimeout):
        database.execute("SELECT 1", timeout_s=30)


def test_clickhouse_connect_timeout_is_still_unreachable() -> None:
    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    database = ClickHouseDatabase("clickhouse://u@ch.example.com:8123/db")
    database._client = httpx.Client(  # noqa: SLF001 - test double for the server
        base_url="http://test", transport=httpx.MockTransport(_unreachable)
    )

    with pytest.raises(ClickHouseError, match="Could not connect"):
        database.execute("SELECT 1", timeout_s=30)


def _clickhouse_answering(response: httpx.Response) -> ClickHouseDatabase:
    database = ClickHouseDatabase("clickhouse://u@ch.example.com:8123/db")
    database._client = httpx.Client(  # noqa: SLF001 - test double for the server
        base_url="http://test",
        transport=httpx.MockTransport(lambda request: response),
    )
    return database


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"X-ClickHouse-Exception-Code": "159"}, "Code: 159. DB::Exception: x"),
        ({}, "Code: 159. DB::Exception: Timeout exceeded. (TIMEOUT_EXCEEDED)"),
    ],
)
def test_clickhouse_timeout_becomes_statement_timeout(
    headers: dict[str, str], body: str
) -> None:
    database = _clickhouse_answering(httpx.Response(500, headers=headers, text=body))

    with pytest.raises(StatementTimeout):
        database.execute("SELECT 1", timeout_s=30)


def test_clickhouse_other_errors_stay_clickhouse_errors() -> None:
    database = _clickhouse_answering(
        httpx.Response(
            404,
            headers={"X-ClickHouse-Exception-Code": "60"},
            text="Code: 60. DB::Exception: Table does not exist. (UNKNOWN_TABLE)",
        )
    )

    with pytest.raises(ClickHouseError):
        database.execute("SELECT 1", timeout_s=30)


def test_databricks_timeout_becomes_statement_timeout(
    monkeypatch: MonkeyPatch,
) -> None:
    error = RuntimeError(
        "[QUERY_EXECUTION_TIMEOUT_EXCEEDED] Query execution was cancelled due to "
        "exceeding the timeout (30s). SQLSTATE: 57KD0"
    )

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            raise error

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "auto_ontology.connectors.databricks.sql.connect", lambda **kwargs: _Conn()
    )
    database = DatabricksDatabase(
        "databricks://token:t@example.databricks.com/main"
        "?http_path=%2Fsql%2F1.0%2Fwarehouses%2Fw"
    )

    with pytest.raises(StatementTimeout):
        database.execute("SELECT 1", timeout_s=30)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[QUERY_EXECUTION_TIMEOUT_EXCEEDED] cancelled after 30s", True),
        ("Query cancelled. SQLSTATE: 57KD0", True),
        ("Query cancelled (sqlState=57KD0)", True),
        # The code inside the statement the warehouse echoed is not a diagnosis.
        ("[UNRESOLVED_COLUMN] `x` in: SELECT * FROM t WHERE c = '57KD0'", False),
    ],
)
def test_databricks_matches_the_timeout_sqlstate_only_as_a_sqlstate(
    message: str, expected: bool
) -> None:
    from auto_ontology.connectors.databricks import _is_statement_timeout

    assert _is_statement_timeout(RuntimeError(message)) is expected
