# SPDX-License-Identifier: Apache-2.0

"""The read-only guard accepts real analytics SQL and refuses everything else."""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.text_to_sql.sql_guard import (
    UnsafeSqlError,
    assert_read_only,
)

ALLOWED = [
    "SELECT 1",
    "SELECT * FROM pagila.public.customer",
    "select count(*) from customer;",
    "WITH t AS (SELECT customer_id, SUM(amount) a FROM payment GROUP BY 1) "
    "SELECT * FROM t ORDER BY a DESC LIMIT 5",
    "SELECT a FROM x UNION ALL SELECT a FROM y",
    "(SELECT a FROM x) EXCEPT (SELECT a FROM y)",
    "SELECT * FROM (SELECT * FROM x) AS s",
    "SELECT * FROM a JOIN b ON a.id = b.a_id WHERE a.v IN (SELECT v FROM c)",
    # Words a keyword filter would trip over: functions, literals, identifiers.
    "SELECT REPLACE(name, 'a', 'b') FROM customer",
    "SELECT * FROM film WHERE title = 'COPY; DROP TABLE x'",
    'SELECT created_at, updated_at, "update" FROM t',
    "SELECT * FROM t WHERE note LIKE '%insert into%'",
    "SELECT EXTRACT(year FROM d), CAST(x AS INT), COALESCE(a, 0) FROM t",
    "SELECT ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) FROM t",
]

REFUSED = [
    ("INSERT INTO t VALUES (1)", "INSERT"),
    ("UPDATE t SET a = 1", "UPDATE"),
    ("DELETE FROM t", "DELETE"),
    ("DROP TABLE t", "DROP"),
    ("TRUNCATE TABLE t", "TRUNCATE"),
    ("CREATE TABLE n AS SELECT * FROM t", "CREATE"),
    ("ALTER TABLE t ADD COLUMN c int", "ALTER"),
    ("GRANT ALL ON t TO public", "GRANT"),
    ("COPY t TO '/tmp/x'", "COPY"),
    ("SET statement_timeout = 0", "SET"),
    ("VACUUM t", "COMMAND"),
    ("CALL p()", "COMMAND"),
    ("EXPLAIN ANALYZE DELETE FROM t", "COMMAND"),
    ("BEGIN", ""),
    # The write is nested: the statement's root is a SELECT.
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM u", "UPDATE"),
    ("WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i", "INSERT"),
    ("SELECT * INTO new_table FROM t", "INTO"),
    ("SELECT * FROM t FOR UPDATE", "LOCK"),
    ("SELECT * FROM t FOR SHARE", "LOCK"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_read_only_queries_pass(sql: str) -> None:
    assert_read_only(sql, "postgres")
    assert_read_only(sql)  # the generic parser agrees


@pytest.mark.parametrize(("sql", "culprit"), REFUSED)
def test_anything_that_writes_is_refused(sql: str, culprit: str) -> None:
    with pytest.raises(UnsafeSqlError) as raised:
        assert_read_only(sql, "postgres")
    assert culprit in str(raised.value).upper()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "SELECT 1; SELECT 2",
        "SELECT * FROM t; DELETE FROM t;",
    ],
)
def test_multiple_statements_are_refused(sql: str) -> None:
    with pytest.raises(UnsafeSqlError, match="single"):
        assert_read_only(sql, "postgres")


@pytest.mark.parametrize("sql", ["", "   ", ";", " ; ; "])
def test_empty_sql_is_refused(sql: str) -> None:
    with pytest.raises(UnsafeSqlError, match="empty"):
        assert_read_only(sql)


def test_sql_that_cannot_be_parsed_is_refused() -> None:
    """Fail closed: what cannot be verified is not run."""
    with pytest.raises(UnsafeSqlError, match="could not be parsed"):
        assert_read_only("SELECT FROM WHERE ((", "postgres")


@pytest.mark.parametrize(
    "call",
    [
        "set_config('a', 'b', false)",
        "pg_terminate_backend(123)",
        "pg_cancel_backend(123)",
        "pg_read_file('/etc/passwd')",
        "lo_import('/etc/passwd')",
        "PG_READ_FILE('/etc/passwd')",
    ],
)
def test_side_effect_functions_are_refused(call: str) -> None:
    with pytest.raises(UnsafeSqlError, match="not allowed"):
        assert_read_only(f"SELECT {call}", "postgres")


def test_a_side_effect_function_nested_in_a_subquery_is_refused() -> None:
    with pytest.raises(UnsafeSqlError, match="pg_terminate_backend"):
        assert_read_only(
            "SELECT * FROM t WHERE id IN (SELECT pg_terminate_backend(pid) FROM s)",
            "postgres",
        )


def test_refusal_message_tells_the_model_what_is_allowed() -> None:
    """The text is fed back to the SQL generator when it repairs the query."""
    with pytest.raises(UnsafeSqlError) as raised:
        assert_read_only("DELETE FROM t")
    assert "read-only SELECT" in str(raised.value)


def test_a_dialect_specific_select_is_judged_in_that_dialect() -> None:
    assert_read_only("SELECT TOP 5 * FROM t", "tsql")
    assert_read_only("SELECT `a` FROM `t` LIMIT 5", "mysql")
