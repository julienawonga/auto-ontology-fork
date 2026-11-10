# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run the SQL a chat turn issues: read-only, logged, and capped in time and rows.

These behaviours deliberately live here rather than in the connectors. A
connector's ``execute`` is the chokepoint for *all* SQL — ingestion
introspection and column profiling included — where neither behaviour is
wanted: logging there buries a question's few statements under hundreds of
housekeeping queries, and a short timeout would break metadata scans over a
large ``information_schema``, which legitimately take minutes.

Only agent-issued SQL reaches the calls here, which is also why the read-only
guard sits here: ingestion legitimately runs other statements through the same
connectors, but nothing the agent writes should ever change the user's data.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from auto_ontology.infra.feature_flags import (
    get_sql_query_timeout_seconds,
    get_sql_result_max_rows,
)
from auto_ontology.retrieval.text_to_sql.sql_guard import (
    UnsafeSqlError,
    assert_read_only,
)

logger = logging.getLogger(__name__)

#: ``DataFrame.attrs`` key set when a result was cut; holds the row limit.
TRUNCATED_AT_ATTR = "truncated_at"

# Generated SQL is normally short, but a pathological statement shouldn't flood
# the log; keep enough to be recognisable and say how much was cut.
_MAX_LOGGED_SQL_CHARS = 4000


def truncate_sql(sql: str) -> str:
    """Return *sql* capped at a length that is safe to log."""
    if len(sql) <= _MAX_LOGGED_SQL_CHARS:
        return sql
    omitted = len(sql) - _MAX_LOGGED_SQL_CHARS
    return f"{sql[:_MAX_LOGGED_SQL_CHARS]}… [{omitted} more chars]"


def log_chat_sql(connector: Any, sql: str, *, kind: str = "chat SQL") -> None:
    """Log one agent-issued statement at INFO, with the credential in use.

    ``auth_mode`` is only exposed by connectors that can run as the calling user
    (currently Databricks); for the rest the credential is fixed and the clause
    is left off rather than stating something misleading.
    """
    database = getattr(connector, "database_name", None) or "unknown"
    auth_mode = getattr(connector, "auth_mode", None)

    if auth_mode:
        logger.info(
            "[%s] Executing %s as %s:\n%s", database, kind, auth_mode, truncate_sql(sql)
        )
    else:
        logger.info("[%s] Executing %s:\n%s", database, kind, truncate_sql(sql))


def execute_chat_sql(
    connector: Any,
    sql: str,
    *,
    kind: str = "chat SQL",
    timeout_s: float | None = None,
) -> pd.DataFrame:
    """Log and run one agent-issued statement: read-only, capped in time.

    Raises:
        UnsafeSqlError: the statement is not a single read-only query. It is
            refused before it reaches the database.

    A user is waiting on the answer and the pipeline allows one stream at a
    time, so a runaway query would pin the slot. The cap is *timeout_s*, or
    when that is ``None`` the SQL Query Timeout from Settings > Agent Settings
    (30s unless an admin changed it, and always 30s without the frontend).
    Connecting and warehouse scheduling may sit outside it, so expect roughly
    the cap plus a couple of seconds of wall clock.

    Every built-in connector honours the cap -- server side where the engine
    has a statement timeout, client side for DuckDB, SQLite and HeavyDB. One
    that doesn't advertise ``supports_statement_timeout`` is called unchanged,
    so the cap never breaks a connector with no way to honour it.

    A connector that advertises ``supports_read_only_execution`` also runs the
    statement read-only (a read-only transaction, session or request setting,
    as the engine allows), so the database refuses a write even if it got past
    the guard.

    The result is cut at the Agent Settings row limit. The returned frame then
    carries the limit in ``frame.attrs[TRUNCATED_AT_ATTR]``, which callers use to
    say the answer is partial; an uncut result has no such attribute. A
    connector with ``supports_row_limit`` stops reading at the limit, any other
    returns everything and is trimmed here.
    """
    try:
        assert_read_only(sql, getattr(connector, "dialect", None))
    except UnsafeSqlError as exc:
        logger.warning(
            "[%s] Refused %s: %s\n%s",
            getattr(connector, "database_name", None) or "unknown",
            kind,
            exc,
            truncate_sql(sql),
        )
        raise

    log_chat_sql(connector, sql, kind=kind)

    options: dict[str, Any] = {}
    if getattr(connector, "supports_statement_timeout", False):
        options["timeout_s"] = (
            get_sql_query_timeout_seconds() if timeout_s is None else timeout_s
        )
    if getattr(connector, "supports_read_only_execution", False):
        options["read_only"] = True
    max_rows = get_sql_result_max_rows()
    if getattr(connector, "supports_row_limit", False):
        options["max_rows"] = max_rows
    return _cut_at(connector.execute(sql, **options), max_rows)


def _cut_at(frame: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    """Trim *frame* to *max_rows* and mark it when anything was dropped."""
    if not isinstance(frame, pd.DataFrame) or len(frame) <= max_rows:
        return frame
    logger.warning("Result cut at %d rows (it had more)", max_rows)
    cut = frame.head(max_rows).copy()
    cut.attrs[TRUNCATED_AT_ATTR] = max_rows
    return cut
