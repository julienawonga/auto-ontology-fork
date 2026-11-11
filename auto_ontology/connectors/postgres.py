# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL connector implementing the :class:`~auto_ontology.connectors.base.SQLDatabase` ABC."""

from __future__ import annotations

import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from auto_ontology.catalog.constants import TableTypes
from auto_ontology.connectors import source_limits
from auto_ontology.connectors.base import SQLDatabase, StatementTimeout


# Maps every table to the relation the catalog knows it by: a partition (at any
# depth) to its topmost partitioned ancestor, anything else to itself.
# ``get_tables`` lists only that root, so a key recorded against a partition
# must be reported under the root's name to match a catalogued table.
#
# Only ``relispartition`` children are followed. Ordinary inheritance
# (``CREATE TABLE child () INHERITS (parent)``) also lives in ``pg_inherits``,
# but such a child is a table in its own right, catalogued under its own name,
# and must keep its own keys.
_PARTITION_ROOTS_CTE = """
    WITH RECURSIVE partition_root(oid, root) AS (
        SELECT c.oid, c.oid
        FROM pg_class c
        WHERE NOT c.relispartition
          AND c.relkind IN ('r', 'p')
        UNION ALL
        SELECT child.oid, parent.root
        FROM partition_root parent
        JOIN pg_inherits i ON i.inhparent = parent.oid
        JOIN pg_class child ON child.oid = i.inhrelid AND child.relispartition
    )
"""


def _fetch_frame(
    conn: psycopg.Connection,
    sql: str,
    parameters: Optional[list],
    max_rows: int | None = None,
) -> pd.DataFrame:
    if max_rows is None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, parameters)
            if cur.description is None:
                return pd.DataFrame()
            return pd.DataFrame(cur.fetchall())
    # A client-side cursor receives the whole result on ``execute``, whatever
    # ``fetchmany`` then asks for, so a capped read needs a server-side
    # (named) cursor: the server hands over only the rows requested. That
    # requires an open transaction, which the caller provides. One row past
    # the cap, so the caller can tell "exactly max_rows" from "more".
    with conn.cursor(name=f"lexis_{uuid.uuid4().hex}", row_factory=dict_row) as cur:
        cur.execute(sql, parameters)
        return pd.DataFrame(cur.fetchmany(max_rows + 1))


class PostgresDatabase(SQLDatabase):
    """Concrete :class:`SQLDatabase` backed by ``psycopg`` (v3).

    Parameters
    ----------
    connection_string:
        A ``libpq``-style connection URI.

        Expected format::

            postgresql://USER:PASSWORD@HOST:5432/DBNAME
    """

    supports_statement_timeout = True
    supports_read_only_execution = True
    supports_row_limit = True

    #: Per-source request limiter; ``None`` (class default) means "no per-source
    #: queueing", which only unit-test doubles built without ``__init__`` rely on.
    _limiter: source_limits.SourceLimiter | None = None

    def __init__(self, connection_string: str) -> None:
        """Record the connection; open nothing.

        No pool, no socket and no thread exist until the first statement, so a
        registered-but-idle source costs the source database zero connections.
        The pool is built on first use (:meth:`_get_pool`) with no minimum size
        and a short idle timeout, so it also gives connections back.
        """
        self._connection_string = connection_string
        self._pool: ConnectionPool | None = None
        self._pool_lock = threading.Lock()
        self._finalizer: weakref.finalize | None = None
        self._limiter = source_limits.SourceLimiter(source_limits.pool_max_size())
        # ``dbname`` in the URI is what ``current_database()`` reports, so the
        # name is known without connecting. Only a URI that omits it falls back
        # to asking the server, lazily, on first access.
        self._database_name: str | None = (
            str(conninfo_to_dict(connection_string).get("dbname") or "") or None
        )

    def _get_pool(self) -> ConnectionPool:
        with self._pool_lock:
            if self._pool is None:
                # Pool transparently replaces connections killed by server
                # timeouts or network middleboxes. ``check_connection`` probes
                # before handing one out; ``max_idle`` releases unused ones
                # (``min_size=0``: all of them) and ``max_lifetime`` recycles
                # before any plausible firewall idle timeout fires.
                #
                # A small pool rather than ``NullPool``-style connect-per-
                # statement: the agent runs several statements per question and
                # ingestion profiling hundreds per table, and each fresh
                # connection costs a TCP + auth (+ TLS) round trip to what is
                # often a remote warehouse. ``max_idle`` is what returns the
                # connections, so idle cost still falls to zero.
                self._pool = ConnectionPool(
                    self._connection_string,
                    kwargs={"autocommit": True},
                    min_size=0,
                    max_size=source_limits.pool_max_size(),
                    max_idle=source_limits.pool_max_idle_s(),
                    max_lifetime=1800.0,
                    num_workers=1,
                    check=ConnectionPool.check_connection,
                    open=True,
                )
                # Close the pool at GC or interpreter exit (whichever comes
                # first) so its worker/scheduler threads stop gracefully;
                # otherwise psycopg_pool warns and stalls shutdown ~5s per
                # thread. Binding ``pool.close`` (not ``self``) keeps the
                # connector GC-eligible.
                self._finalizer = weakref.finalize(self, self._pool.close)
            return self._pool

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        """Check a connection out, waiting for a free slot rather than failing.

        See :mod:`auto_ontology.connectors.source_limits`: the per-source and
        process-wide slots are taken first, so the pool itself never blocks.
        """
        limiter = self._limiter
        if limiter is None:
            with self._get_pool().connection() as conn:
                yield conn
            return
        with limiter.slot(self._label()):
            with self._get_pool().connection() as conn:
                yield conn

    def _label(self) -> str:
        return self._database_name or "<unnamed>"

    @property
    def dialect(self) -> str:
        """Return this engine's sqlglot dialect name.

        Must be a member of ``sqlglot.dialects.DIALECTS`` — callers pass it
        straight to sqlglot without translation. See ``CONNECTOR_REGISTRY``.
        """
        return "postgres"

    @property
    def database_name(self) -> str:
        if self._database_name is None:
            self._database_name = str(
                self.execute("SELECT current_database()").iloc[0, 0]
            )
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
        max_rows: int | None = None,
    ) -> pd.DataFrame:
        """Run *sql*; with *timeout_s*, the server cancels it after that long.

        The cap is ``SET LOCAL statement_timeout`` inside a transaction, so it
        ends with the statement and never leaks onto the pooled connection.

        With *read_only* the statement runs in a ``READ ONLY`` transaction, which
        Postgres enforces itself: any write fails with ``ReadOnlySqlTransaction``
        whatever the statement looked like to the parser. The pool is in
        autocommit mode, so the setting is scoped to this one transaction and
        never reaches the next caller of the pooled connection.

        With *max_rows* at most ``max_rows + 1`` rows are read, through a
        server-side cursor so the rest is never sent; the extra one tells the
        caller the result was cut.

        Raises:
            StatementTimeout: the server cancelled the statement at the cap.
        """
        with self._connection() as conn:
            if timeout_s is None and not read_only and max_rows is None:
                return _fetch_frame(conn, sql, parameters)
            started = time.monotonic()
            try:
                with conn.transaction():
                    if read_only:
                        # Must precede the first query of the transaction.
                        conn.execute("SET TRANSACTION READ ONLY")
                    if timeout_s is not None:
                        conn.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (f"{max(1, int(timeout_s * 1000))}ms",),
                        )
                    return _fetch_frame(conn, sql, parameters, max_rows)
            except psycopg.errors.QueryCanceled as exc:
                # QueryCanceled also covers pg_cancel_backend(); only the
                # timeout we set is ours to relabel.
                if timeout_s is None or "statement timeout" not in str(exc):
                    raise
                raise StatementTimeout.since(timeout_s, started) from exc

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def get_tables(self) -> pd.DataFrame:
        """Every relation a user can query, one row each.

        Driven from ``pg_class`` rather than ``information_schema.tables``,
        because that view omits materialized views entirely — so a matview could
        never reach the catalog no matter what ``relkind`` was allowed through,
        and :data:`TableTypes.MATERIALIZED_VIEW` was unreachable.

        ``relkind`` cases, all of which are queryable and belong in the catalog:
          r  ordinary table          p  partitioned table (the parent)
          v  view                    m  materialized view
          f  foreign table

        ``relispartition = false`` drops partition *children*: they are an
        implementation detail of their parent, and listing both would show the
        same rows twice under names no one writes queries against. The parent
        ``p`` must be kept for exactly that reason — excluding both, as this did
        before, made partitioned tables invisible.
        """
        view_type = TableTypes.VIEW
        materialized_view_type = TableTypes.MATERIALIZED_VIEW
        base_table_type = TableTypes.BASE_TABLE
        return self.execute(f"""
            SELECT
                n.nspname   AS table_schema,
                c.relname   AS table_name,
                CASE c.relkind
                    WHEN 'v' THEN '{view_type}'
                    WHEN 'm' THEN '{materialized_view_type}'
                    ELSE '{base_table_type}'
                END AS table_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'
              AND c.relispartition = false
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              -- Restores what information_schema filtered for free: a relation
              -- the ingesting role cannot read must not be catalogued, or
              -- text-to-SQL will offer it and every query dies with
              -- "permission denied".
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY n.nspname, c.relname
        """)

    def get_columns(self) -> pd.DataFrame:
        """Columns for every relation :meth:`get_tables` returns.

        Also driven from ``pg_catalog``, and for the same reason: matview
        columns are absent from ``information_schema.columns``, so a matview
        that reached the catalog would arrive with no columns at all.

        ``format_type(atttypid, NULL)`` gives the unqualified type name without
        precision (``character varying``, not ``character varying(255)``). It is
        **not** identical to ``information_schema.columns.data_type``, which
        flattens a domain to its base type and reports ``ARRAY`` /
        ``USER-DEFINED`` where this reports ``text[]`` / ``mpaa_rating``. The
        more specific name is deliberate — it is what the text-to-SQL agent
        needs to write a valid predicate against an enum or an array column.

        ``ordinal_position`` is a row number, not ``attnum``. ``attnum`` keeps
        the slots of dropped columns, so a table that has ever had a
        ``DROP COLUMN`` would number its columns 1, 3, 4 and disagree with both
        ``information_schema`` and the model-interchange export.

        ``has_table_privilege`` restores the filtering ``information_schema``
        applied for free: without it, a role with rights on one schema still
        catalogues, embeds and offers every table it cannot read, and every
        generated query against them dies with ``permission denied``.
        """
        return self.execute("""
            SELECT
                n.nspname                        AS table_schema,
                c.relname                        AS table_name,
                a.attname                        AS column_name,
                format_type(a.atttypid, NULL)    AS data_type,
                NOT a.attnotnull                 AS is_nullable,
                row_number() OVER (
                    PARTITION BY c.oid ORDER BY a.attnum
                )                                AS ordinal_position
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'
              AND c.relispartition = false
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND has_table_privilege(c.oid, 'SELECT')
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
        """)

    def get_queries(self, hours: int = 24) -> pd.DataFrame:
        """Return recent queries from ``pg_stat_activity``.

        Uses ``state_change`` as ``end_time`` and filters to the last ``hours``
        hours. Excludes the current backend and rows with no recorded query.
        """
        try:
            # Todo: Add filter of exclude information schema and pg_catalog tables
            return self.execute(
                """
                SELECT
                    state_change AS end_time,
                    query        AS query_text
                FROM pg_stat_activity
                WHERE pid != pg_backend_pid()
                  AND query IS NOT NULL
                  AND query <> ''
                  AND state_change >= now() - make_interval(hours => %s)
                ORDER BY state_change DESC
                """,
                [hours],
            )
        except psycopg.Error:
            return pd.DataFrame(columns=["end_time", "query_text"])

    def get_views(self) -> pd.DataFrame:
        return self.execute("""
            SELECT
                v.table_schema     AS table_schema,
                v.table_name       AS table_name,
                v.view_definition  AS view_definition
            FROM information_schema.views v
            WHERE v.table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY v.table_schema, v.table_name
        """)

    def get_pks(self) -> pd.DataFrame:
        """Primary-key columns, one row each, under the name the catalog uses.

        Driven from ``pg_constraint`` rather than ``information_schema``, for
        the reasons given in :meth:`get_fks`: a key declared on partition
        children is reported for the partitioned parent (the only name
        :meth:`get_tables` catalogues), and constraints are matched by OID, not
        by a name that is only unique per table.

        ``ordinal_position`` is the column's position within the key, as
        before. ``DISTINCT`` folds the copies Postgres keeps on every partition
        of a key declared on the parent, and the identical keys of children
        that each declare their own.
        """
        return self.execute(f"""
            {_PARTITION_ROOTS_CTE}
            SELECT DISTINCT
                n.nspname                AS table_schema,
                c.relname                AS table_name,
                a.attname                AS column_name,
                k.ord                    AS ordinal_position
            FROM pg_constraint con
            CROSS JOIN LATERAL unnest(con.conkey)
                WITH ORDINALITY AS k(attnum, ord)
            JOIN partition_root src ON src.oid = con.conrelid
            JOIN pg_class c ON c.oid = src.root
            JOIN pg_namespace n ON n.oid = c.relnamespace
            -- Names are read from the relation that owns the constraint: a
            -- partition shares its parent's column names, not its attnums.
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            WHERE con.contype = 'p'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY table_schema, table_name, ordinal_position, column_name
        """)

    def get_fks(self) -> pd.DataFrame:
        """Foreign-key column pairs, one row per (column, referenced column).

        Driven from ``pg_constraint``. The previous query joined
        ``information_schema.key_column_usage`` to ``constraint_column_usage``
        on constraint name and schema, which was wrong three ways:

        * **Partitions.** A key declared on partition children came back under
          the child's name. :meth:`get_tables` catalogues only the parent, so
          those rows matched no table and the relationship was lost — Pagila's
          ``payment`` declares all its keys that way and had none. Both ends
          are now mapped to their partition root.
        * **Composite keys.** ``constraint_column_usage`` carries no position,
          so ``(a, b) -> (x, y)`` came back as the cross product ``a->x, a->y,
          b->x, b->y``. ``conkey`` and ``confkey`` are parallel arrays; they are
          unnested together so each column meets only its counterpart.
        * **Constraint names.** Postgres makes them unique per table, not per
          schema, so two tables reusing a name had their columns mixed.
          Constraints are matched by OID here.

        ``DISTINCT`` folds the copies Postgres keeps per partition (on either
        end) of a key declared on a partitioned table, and the identical keys
        of sibling partitions. The schema and privilege filters mirror
        :meth:`get_tables`, so no row names a relation the catalog lacks.
        """
        return self.execute(f"""
            {_PARTITION_ROOTS_CTE}
            SELECT DISTINCT
                n.nspname                AS table_schema,
                c.relname                AS table_name,
                a.attname                AS column_name,
                rn.nspname               AS referenced_schema,
                rc.relname               AS referenced_table,
                ra.attname               AS referenced_column
            FROM pg_constraint con
            CROSS JOIN LATERAL unnest(con.conkey, con.confkey)
                AS k(attnum, ref_attnum)
            JOIN partition_root src ON src.oid = con.conrelid
            JOIN pg_class c ON c.oid = src.root
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN partition_root dst ON dst.oid = con.confrelid
            JOIN pg_class rc ON rc.oid = dst.root
            JOIN pg_namespace rn ON rn.oid = rc.relnamespace
            -- Names are read from the relations the constraint itself points
            -- at: a partition shares its parent's column names, not its
            -- attnums.
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            JOIN pg_attribute ra
              ON ra.attrelid = con.confrelid AND ra.attnum = k.ref_attnum
            WHERE con.contype = 'f'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'
              AND has_table_privilege(c.oid, 'SELECT')
              AND has_table_privilege(rc.oid, 'SELECT')
            ORDER BY table_schema, table_name, column_name,
                     referenced_schema, referenced_table, referenced_column
        """)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> None:
        """Verify connectivity and that the catalog is readable.

        Opens a throwaway connection (this checks the credentials, not a pooled
        session), but still under the process-wide cap.
        """
        with source_limits.global_slot(self._label()):
            with psycopg.connect(self._connection_string) as conn:
                conn.execute("SELECT schema_name FROM information_schema.schemata")

    def close(self) -> None:
        finalizer = self._finalizer
        if finalizer is not None:
            finalizer.detach()
        pool = self._pool
        if pool is not None and not pool.closed:
            pool.close()
