<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Safety of agent-issued SQL

The chat runs SQL that a language model wrote, against your database. Three
protections apply to every statement the agent issues. They sit in
`execute_chat_sql`, the single place agent SQL passes through, so ingestion,
which uses the same connectors, is not affected.

None of them replaces a least-privilege database user. Connect Lexis with an
account that can only read what the chat should see.

## 1. Only one read-only query is accepted

Before a statement reaches the database it is parsed, and refused unless it is
a single `SELECT` (a `WITH` or `UNION` of selects included). The check walks
the whole syntax tree, so these are refused too:

- a write nested in a CTE, such as `WITH d AS (DELETE ... RETURNING *) SELECT ...`
- `SELECT ... INTO new_table`
- row locks (`FOR UPDATE`, `FOR SHARE`)
- several statements in one string
- statements that are not queries (`COPY`, `VACUUM`, `CALL`, `SET`, ...)
- a few server-side functions that act outside the data, such as
  `pg_terminate_backend`, `pg_read_file` and `set_config`

SQL that cannot be parsed is refused. The refusal message is returned to the
model, which uses it to rewrite the query.

## 2. The database refuses writes

On connectors that support it, the statement also runs in a read-only mode
enforced by the database, so a write is refused even if it got past the parser.

| Connector | Mechanism |
|---|---|
| PostgreSQL | `SET TRANSACTION READ ONLY`, in a transaction that holds only that statement |
| MySQL, MariaDB | `SET SESSION TRANSACTION READ ONLY` on the connection the call owns |
| SQLite | `PRAGMA query_only = ON` for the call |
| DuckDB | `BEGIN TRANSACTION READ ONLY` on a private cursor |
| ClickHouse | `readonly=1` on that request |
| Trino | `START TRANSACTION READ ONLY` per statement, rolled back afterwards |
| Snowflake, Databricks, Kyuubi, HeavyDB | none: they rely on the first protection |

Notes:

- **ClickHouse:** under `readonly=1`, a query that carries its own `SETTINGS`
  clause or uses `file()`, `url()` or `s3()` is refused. An account whose
  profile is already `readonly=2` is run under that profile.
- **DuckDB:** the read-only transaction protects the main and attached
  databases. `COPY ... TO` a file and `ATTACH` are refused by the first
  protection, not by this one.
- **MySQL and ClickHouse** were verified by hand against real servers; the
  automated tests use a fake driver for them.
- **Trino:** the engine refuses a write or DDL in the read-only transaction
  (`READ_ONLY_VIOLATION`) and a `CALL` ("Procedures cannot be called within a
  transaction"). It was verified against a real Trino 470 server on the
  `memory` catalog. A statement that is refused aborts its transaction, so
  each statement gets its own. `SET SESSION` is not blocked by it (the first
  protection refuses it).

## 3. Results are capped

A statement returns at most `sql_result_max_rows` rows: 10,000 by default,
between 1 and 1,000,000. It is read for each statement from the
`sql_result_max_rows` row of the configuration table. Admins set it in the
SQL Result Row Limit field on Settings > Agent Settings; without that row the
default applies, and so does a stored value outside the bounds.

When a result is cut, the answer says so ("the result was limited to the first
N rows, so the figures above may be incomplete") and the response carries
`result_truncated_at`. A cut result is never presented as complete.

PostgreSQL stops reading at the limit through a server-side cursor. The other
connectors read the whole result and are trimmed afterwards, so the limit
bounds what the model and the interface receive but not the memory used to
fetch it.

The existing SQL Query Timeout (Settings > Agent Settings, 30 seconds by
default) still applies.
