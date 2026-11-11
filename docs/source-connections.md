<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Source-database connections

How Lexis uses connections to the databases you register under **Settings →
Connections** (the *sources*), and how to size them. The Lexis store (the
Postgres Lexis itself runs on) has its own pool, set by
`AUTO_ONTOLOGY_PG_POOL_SIZE` / `AUTO_ONTOLOGY_PG_MAX_OVERFLOW`, and is not covered here.

## Behaviour

- **An idle source holds no connection.** A PostgreSQL source connector opens
  nothing when it is created: no socket, no pool, no thread. Its database name is
  read from the connection URL, not from the server. The pool is built on the
  first statement, starts empty (`min_size=0`) and closes a connection after
  `SOURCE_POOL_MAX_IDLE_S` of disuse, so a source that stops being queried returns
  to zero.
- **A small pool, not connect-per-statement.** `NullPool`-style (one fresh
  connection per statement) would also hold nothing while idle, but every statement
  would pay a TCP + authentication (+ TLS) round trip, often to a remote warehouse.
  The agent runs several statements per question and ingestion profiling hundreds per
  table. A pool of at most `SOURCE_POOL_MAX_SIZE` connections with a short idle
  timeout reuses connections during bursts and still returns them afterwards.
- **A cap, and waiting instead of failing.** At most `SOURCE_MAX_CONNECTIONS`
  statements hold a source connection at once, summed over all sources, in one
  process; one source runs at most `SOURCE_POOL_MAX_SIZE` at once. A request over
  either limit **waits** up to `SOURCE_CONNECTION_WAIT_S`. Past that it fails with
  `SourceConnectionTimeout`, a message that says what was waited for and which
  variables to raise. It is never turned into an empty connector list.
- **Errors surface as themselves.** If the connection list cannot be read from the
  store, `get_connectors()` raises `ConnectorLoadError` (with the store's own error
  as the cause) rather than returning `[]`, which used to reach the user as the
  unrelated "missing required 'connectors'". "Nothing is configured" is still `[]`.
  A connector that fails to build raises its own error.
- **Read-only and the row cap are unchanged** (see [query-safety.md](./query-safety.md)):
  the same read-only transaction and server-side cursor run inside the checked-out connection.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SOURCE_POOL_MAX_SIZE` | `2` | Connections one source's pool may open; also its concurrent statements. |
| `SOURCE_POOL_MAX_IDLE_S` | `30` | Seconds an unused connection stays open before it is closed. |
| `SOURCE_MAX_CONNECTIONS` | `12` | Cap on connections in use at once across all sources, **per process**. |
| `SOURCE_CONNECTION_WAIT_S` | `60` | Seconds a request waits for a free connection before `SourceConnectionTimeout`. |

Values are read at call time. A value that is not a positive number is ignored with
a warning and the default is used; the cap cannot be disabled by a typo.

## Sizing against a source's `max_connections`

The cap is **per process**. The backend, the ingestion service and every spawned chat
worker (the warm standby plus one per in-flight chat) are separate processes, each
with its own cap and its own pools. The worst case a source database can see is

```
(processes using it) x min(SOURCE_MAX_CONNECTIONS, SOURCE_POOL_MAX_SIZE)
```

in use at once, plus connections idle for up to `SOURCE_POOL_MAX_IDLE_S`. With the
defaults, 14 concurrent chats plus the backend and ingestion is up to 16 processes
x 2 = 32 connections to one source in the worst case, which is the number to compare
with that server's `max_connections`. Lower `SOURCE_POOL_MAX_SIZE` to 1 if that is
too many.

Scope of this change: only the PostgreSQL connector pools connections and is
capped here. MySQL opens one connection per call and holds nothing idle but is not
yet counted against the cap. Other engines are unchanged.

## Checking it on a stack

```bash
# Idle: connections to the sources should be ~0 (the query excludes itself)
psql -h <source-host> -U <user> -d postgres -Atc \
  "SELECT datname, count(*) FROM pg_stat_activity WHERE datname IS NOT NULL AND pid <> pg_backend_pid() GROUP BY 1"
```

The CI tests for this are `auto_ontology/connectors/tests/test_source_connection_pooling.py`
(real Postgres) and `test_source_limits.py` (no database).
