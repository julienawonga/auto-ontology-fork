# SPDX-FileCopyrightText: Copyright (c) 2024-25, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""DuckDB connector for in-process SQL execution.

Wraps ``duckdb.connect()`` with helpers to register pandas DataFrames or
scan CSV/Parquet/JSON files directly from the filesystem.  No server or Docker
service is required — DuckDB runs fully in-process.

This is the reference implementation of
:class:`~auto_ontology.connectors.base.SQLDatabase`.

Example
-------
::

    from duckdb_connector import DuckDB  # run from tabular-dev-tools/

    conn = DuckDB("./spider2.duckdb")
    rows = conn.execute("SELECT * FROM Airlines.flights LIMIT 5")
    # rows -> [{"flight_id": 1, ...}]
"""

from __future__ import annotations

import itertools
import logging
import threading
from datetime import datetime
from pathlib import Path
import duckdb
import pandas as pd
from typing import Optional

from auto_ontology.connectors.base import SQLDatabase, StatementTimeout

logger = logging.getLogger(__name__)

_EPHEMERAL_COUNTER = itertools.count()


def _execute(
    conn: duckdb.DuckDBPyConnection, sql: str, parameters: Optional[list]
) -> pd.DataFrame:
    if parameters:
        rel = conn.execute(sql, parameters)
    else:
        rel = conn.execute(sql)
    return rel.df()


def _execute_capped(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Optional[list],
    timeout_s: float,
) -> pd.DataFrame:
    """Run *sql* on *cursor*, interrupting it after *timeout_s* seconds."""
    fired = threading.Event()

    def _interrupt() -> None:
        fired.set()
        cursor.interrupt()

    timer = threading.Timer(timeout_s, _interrupt)
    timer.daemon = True
    timer.start()
    try:
        return _execute(cursor, sql, parameters)
    except duckdb.InterruptException as exc:
        if fired.is_set():
            raise StatementTimeout(timeout_s) from exc
        raise
    finally:
        timer.cancel()
        timer.join()


class DuckDBDatabase(SQLDatabase):
    """In-process DuckDB connection with convenience helpers.

    Parameters
    ----------
    database:
        Path to a persistent DuckDB database file, or ``None`` / ``":memory:"``
        for an ephemeral in-memory database (default: in-memory).
    read_only:
        Open the database in read-only mode (default: True).  Multiple
        processes can hold a read-only connection simultaneously; set to
        ``False`` only when you need to write to the file.
    """

    supports_statement_timeout = True
    supports_read_only_execution = True

    def __init__(self, connection_string: str, *, read_only: bool = True) -> None:
        db_path = connection_string
        if db_path.startswith("duckdb://"):
            db_path = db_path[len("duckdb://") :]
        self.conn = duckdb.connect(database=db_path, read_only=read_only)
        self._connection_string: str = (
            f":memory:{next(_EPHEMERAL_COUNTER)}"
            if db_path in ("", ":memory:")
            else str(Path(db_path).resolve())
        )
        self._database_name: str = self.execute("SELECT current_database()").iloc[0, 0]
        logger.debug(
            "DuckDB connected (database=%r, read_only=%s).",
            self._database_name,
            read_only,
        )

    @property
    def dialect(self) -> str:
        """Return this engine's sqlglot dialect name.

        Must be a member of ``sqlglot.dialects.DIALECTS`` — callers pass it
        straight to sqlglot without translation. See ``CONNECTOR_REGISTRY``.
        """
        return "duckdb"

    @property
    def database_name(self) -> str:
        return self._database_name

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        sql: str,
        parameters: Optional[list] = None,
        *,
        timeout_s: float | None = None,
        read_only: bool = False,
    ) -> pd.DataFrame:
        """Execute a SQL statement and return a pandas DataFrame.

        Parameters
        ----------
        sql:
            SQL query to execute.
        parameters:
            Optional positional parameters.
        timeout_s:
            Interrupt the statement after this many seconds and raise
            :class:`StatementTimeout`. DuckDB has no server-side cap, so a
            timer thread calls ``interrupt()``.
        read_only:
            Run the statement in a read-only transaction, in which DuckDB
            refuses to write to any attached database. The connector opens
            its file read-only by default, which already refuses writes; this
            also covers one opened with ``read_only=False`` and in-memory
            databases, which cannot be opened read-only at all. It protects
            the databases only: ``COPY ... TO`` a file and ``ATTACH`` still
            run in such a transaction and are left to the parse-time guard.
        """
        logger.debug("DuckDB executing (→ DataFrame): %s", sql[:200])
        if timeout_s is None and not read_only:
            return _execute(self.conn, sql, parameters)

        # ``interrupt()`` stops whatever is running on the connection it is
        # called on, and profiling runs tables in parallel. A cursor of its own
        # (same database, separate client context) means the timer can only
        # ever stop this statement, and a read-only transaction only ever
        # holds this statement.
        cursor = self.conn.cursor()
        try:
            if read_only:
                # Absent from DuckDB's user documentation, but in its grammar
                # and its own suite
                # (test/sql/transactions/test_read_only_transactions.test), and
                # exercised against the real engine in ours. Closing the cursor
                # discards the transaction.
                cursor.execute("BEGIN TRANSACTION READ ONLY")
            if timeout_s is None:
                return _execute(cursor, sql, parameters)
            return _execute_capped(cursor, sql, parameters, timeout_s)
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_tables(self) -> pd.DataFrame:
        """Return all tables from information_schema as a DataFrame."""
        return self.execute(
            """
            SELECT
                table_schema,
                table_name
            FROM information_schema.tables
            ORDER BY table_schema, table_name
        """
        )

    def get_columns(self) -> pd.DataFrame:
        """Return all columns from information_schema as a DataFrame."""
        return self.execute(
            """
            SELECT
                table_schema,
                table_name,
                column_name,
                data_type,
                is_nullable = 'YES' AS is_nullable,
                ordinal_position
            FROM information_schema.columns
            ORDER BY table_schema, table_name, ordinal_position
        """
        )

    def get_queries(self) -> pd.DataFrame:
        """DuckDB has no built-in query history — loads sample queries from a CSV."""
        csv_path = (
            Path(__file__).parent
            / "benchmarks"
            / self._database_name
            / "sample_queries.csv"
        )
        if not csv_path.exists():
            logger.warning(
                "No sample queries CSV found at %s; returning empty DataFrame.",
                csv_path,
            )
            return pd.DataFrame(columns=["query_text", "end_time"])
        df = pd.read_csv(csv_path)
        df["end_time"] = datetime.today()
        return df

    def get_views(self) -> pd.DataFrame:
        """Return all views from information_schema."""
        return self.execute(
            """
            SELECT
                table_schema,
                table_name,
                view_definition
            FROM information_schema.views
            ORDER BY table_catalog, table_schema, table_name
        """
        )

    def get_pks(self) -> pd.DataFrame:
        return pd.DataFrame()

    def get_fks(self) -> pd.DataFrame:
        return pd.DataFrame()

    # ------------------------------------------------------------------
    # Context manager / cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the DuckDB connection."""
        self.conn.close()
