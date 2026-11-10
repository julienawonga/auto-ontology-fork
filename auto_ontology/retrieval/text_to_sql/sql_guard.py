# SPDX-License-Identifier: Apache-2.0

"""Refuse agent-issued SQL that is not one read-only query.

The text-to-SQL agent runs whatever an LLM wrote against the user's database,
so "read-only" must not depend on the model behaving or on how the connection
user happens to be granted. This is the first of two layers:

1. **This guard** parses the statement and refuses anything that is not a single
   ``SELECT`` (a ``WITH``/``UNION`` of selects included). It works on the syntax
   tree, not on keywords, so ``REPLACE(name, ...)`` or a ``'copy'`` literal is
   fine while ``WITH x AS (DELETE ... RETURNING *) SELECT ...`` is not -- the
   root of that one is a ``SELECT``, the write is nested.
2. **The connector** runs the statement in a read-only transaction when it can
   (see ``supports_read_only_execution``), so the database itself refuses a
   write that slips past the parser.

Neither replaces a least-privilege database user; both exist so that a mistake
in one place is not a data-loss incident.

The guard fails closed: SQL that cannot be parsed is refused.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# The only statement shapes the agent may run.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.SetOperation,  # UNION / INTERSECT / EXCEPT
    exp.Subquery,  # a parenthesised select
)

# Nodes that must not appear *anywhere* in the tree. Checking only the root
# would miss a data-modifying CTE (``WITH d AS (DELETE ...) SELECT ...``).
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Grant,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.Analyze,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    # Statements sqlglot does not model (VACUUM, CALL, DO, EXPLAIN ANALYZE, ...)
    # come back as a generic Command; none is a query.
    exp.Command,
    # ``SELECT ... INTO new_table`` creates a table.
    exp.Into,
    # ``FOR UPDATE`` / ``FOR SHARE`` take row locks.
    exp.Lock,
)

# Functions with side effects outside the data that a read-only transaction
# does not stop: they act on the server, its files or other sessions.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
    }
)


class UnsafeSqlError(ValueError):
    """The SQL is not a single read-only query.

    The message is written for the SQL-generation model, which sees it when the
    pipeline asks it to repair the query: say what is wrong and what is allowed.
    """


def _describe(node: exp.Expression) -> str:
    return type(node).__name__.upper()


def _function_name(func: exp.Func) -> str:
    if isinstance(func, exp.Anonymous):
        return str(func.name).lower()
    return func.sql_name().lower()


def assert_read_only(sql: str, dialect: str | None = None) -> None:
    """Raise :class:`UnsafeSqlError` unless *sql* is one read-only ``SELECT``.

    *dialect* is the connector's sqlglot dialect name; ``None`` or ``""`` means
    the generic parser.
    """
    if not sql or not sql.strip():
        raise UnsafeSqlError("The SQL is empty.")

    try:
        statements = sqlglot.parse(sql, read=dialect or None)
    except SqlglotError as exc:
        raise UnsafeSqlError(
            f"The SQL could not be parsed, so it cannot be verified as read-only: {exc}"
        ) from exc

    # ``parse`` yields None for an empty statement such as a trailing ';'.
    parsed = [s for s in statements if s is not None]
    if not parsed:
        raise UnsafeSqlError("The SQL is empty.")
    if len(parsed) > 1:
        raise UnsafeSqlError(
            f"Only a single SQL statement may be run; found {len(parsed)}."
        )

    tree = parsed[0]
    if not isinstance(tree, _ALLOWED_ROOTS):
        raise UnsafeSqlError(
            f"Only a read-only SELECT query may be run; found {_describe(tree)}."
        )

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSqlError(
                "Only a read-only SELECT query may be run; "
                f"it contains {_describe(node)}."
            )
        if isinstance(node, exp.Func) and _function_name(node) in _FORBIDDEN_FUNCTIONS:
            raise UnsafeSqlError(
                f"The function {_function_name(node)}() is not allowed in a "
                "read-only query."
            )
