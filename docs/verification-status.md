<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Verification status

What each capability of this fork has actually been checked against. A claim
here is only as strong as its **Verification level**; a feature is not "done"
because its unit tests pass, and a number is not a measurement because a script
printed it. Every pull request that adds or changes a capability updates its row
(see the pull request template). `dev_tools/integrity/tests/test_register.py`
fails CI when a row is malformed or cites a repository path that does not exist.

## Verification levels

Use the **lowest** level that is true for everything the claim says. If a claim
has a part verified for real and a part verified with fakes, split it into two
rows rather than rounding up.

| Level | Meaning |
|---|---|
| `REAL-E2E` | Exercised end to end on a real running stack: the real database, the real model endpoint and the real services, through the path a user takes. |
| `REAL-ENGINE` | Run against a real database engine or server (not an in-memory stand-in), but not through the full stack. |
| `FAKES` | Only unit tests with fakes, stubs, mocks or recorded data. A real parser or library is not a real engine: the code was never run against the system it talks to. |
| `UNVERIFIED` | Written, not exercised. |

Columns: **Evidence** must be checkable: a test path in backticks (the
`dev_tools/integrity` test checks that it exists), a pull request number, or an
eval report label. Manual runs that no one can repeat from the repository say so
in the claim. **Last verified** is the date the level was last confirmed by
running something, or `never`; rows seeded on 2026-11-10 carry that date because
the maintainer reviewed them that day, not because everything was re-run.

## Register

| Capability | Claim | Verification level | Evidence | Last verified |
|---|---|---|---|---|
| Parse-time read-only SQL guard | Agent SQL that is not a single read-only SELECT is refused before it reaches a database (nested writes, SELECT INTO, row locks, several statements, unparseable SQL). The tests use the real sqlglot parser but never execute against a database, so what the guard accepts is not cross-checked against a real engine. | FAKES | `auto_ontology/retrieval/text_to_sql/tests/test_sql_guard.py`, PR #8 | 2026-11-10 |
| Postgres read-only transaction | A write issued through agent SQL is refused by PostgreSQL itself, and the setting does not leak to the pooled connection. | REAL-ENGINE | `auto_ontology/connectors/tests/test_statement_timeouts.py` (live tests, run in CI against pgvector Postgres 17), PR #8 | 2026-11-10 |
| SQLite and DuckDB read-only | The database refuses a write (SQLite `query_only`, DuckDB read-only transaction) and the mode does not outlive the statement. | REAL-ENGINE | `auto_ontology/connectors/tests/test_statement_timeouts.py` (real in-process engines), PR #14 | 2026-11-10 |
| MySQL and MariaDB read-only, in CI | `SET SESSION TRANSACTION READ ONLY` is issued on the connection that runs the statement. CI uses a fake driver, so it checks the calls made, not the server's refusal. The opt-in live test (`AUTO_ONTOLOGY_LIVE_MYSQL`) is skipped in CI. | FAKES | `auto_ontology/connectors/tests/test_mysql.py`, PR #14 | 2026-11-10 |
| MySQL and MariaDB read-only, manual | Verified by hand against MySQL 8.4 and MariaDB 11.8 in throwaway containers. One-off: nothing in the repository reproduces it. | REAL-ENGINE | PR #14, `docs/query-safety.md` (manual, not repeatable from CI) | 2026-11-10 |
| ClickHouse read-only, in CI | `readonly=1` is sent with the request, with a fallback for `readonly=2` accounts. CI uses a fake client. | FAKES | `auto_ontology/connectors/tests/test_clickhouse.py`, PR #14 | 2026-11-10 |
| ClickHouse read-only, manual | Verified by hand against ClickHouse 22.8 and 26.10 in throwaway containers. One-off: nothing in the repository reproduces it. | REAL-ENGINE | PR #14, `docs/query-safety.md` (manual, not repeatable from CI) | 2026-11-10 |
| Trino read-only | `START TRANSACTION READ ONLY` per statement refuses writes, DDL and CALL. Run against Trino 470 with the `memory` catalog only; other catalogs are untested. The live test is opt-in (`TRINO_TEST_URL`) and skipped in CI. | REAL-ENGINE | `auto_ontology/connectors/tests/test_statement_timeouts.py`, PR #21 | 2026-11-10 |
| Snowflake, Databricks, Kyuubi, HeavyDB read-only | These connectors have no database-enforced read-only mode; they rely on the parse-time guard alone. | UNVERIFIED | `auto_ontology/connectors/tests/test_statement_timeouts.py` (asserts only that they do not advertise read-only execution) | 2026-11-10 |
| Result row cap, PostgreSQL | At most `sql_result_max_rows` rows are read, through a server-side cursor, so a huge result does not load into memory. | REAL-ENGINE | `auto_ontology/connectors/tests/test_statement_timeouts.py` (`test_live_postgres_capped_read_does_not_load_the_whole_result`), PR #9 | 2026-11-10 |
| Result row cap, other connectors | The whole result is fetched and trimmed afterwards: the model and the interface get at most the cap, but memory used to fetch is not bounded. | FAKES | `auto_ontology/retrieval/text_to_sql/tests/test_chat_sql.py`, PR #9 | 2026-11-10 |
| Any OpenAI-compatible model provider | Chat, embedding (configurable width and extra fields) and rerank go through an OpenAI-compatible gateway. The configuration code is unit-tested; the gateway is the maintainer's local stack. | REAL-E2E | `auto_ontology/utils/tests/test_model_config.py`, `auto_ontology/utils/tests/test_embedding.py`, PR #11 | 2026-11-10 |
| Semantic embedding with the configured parameters | The semantic embedder uses `EMBED_EXTRA_FIELDS` and `EMBED_DIMENSIONS`. Re-embedded 80 rows on the running stack. | REAL-E2E | PR #11, `auto_ontology/semantic/tests/test_embed.py` | 2026-11-10 |
| Semantic index status | `GET /api/semantic-compilation/status` reports per database `expected`, `indexed` and `missing`. Read 80 of 80 on the running stack. | REAL-E2E | PR #18, `auto_ontology/server/tests/test_semantic_compilation.py` | 2026-11-10 |
| Semantic index reindex route | `POST /api/semantic-compilation/reindex` rebuilds a database's index. Tested with fakes plus database tests; never called against the running stack. | FAKES | PR #18, `auto_ontology/server/tests/test_semantic_compilation.py` | 2026-11-10 |
| Junction far-side tables and ranking projection | The table on the other side of a kept junction, and the ranked value next to the item, reach SQL generation. Measured on the Pagila questions that motivated both changes: 61.5% to 94.9%. LIMITATION: those are the development questions; there is no held-out evidence for these two changes. | REAL-E2E | PRs #13 and #15, eval run labelled `candidate-junction+projection` (report not committed), `auto_ontology/dal/tests/test_attributes.py` | 2026-11-10 |
| Postgres FK/PK extraction | Keys declared on partitions are reported for the parent, composite keys pair by position, cross-schema keys are kept. Live Pagila and Postgres 17 tests. A catalog ingested before the fix keeps its old keys: the running catalog has not been re-ingested. | REAL-ENGINE | `auto_ontology/connectors/tests/test_postgres_keys.py`, PR #16 | 2026-11-10 |
| Exclude views from ingestion | `exclude_views` skips views and materialized views by type. FORK.md states it was checked on Pagila (15 tables kept, 7 views skipped); the repository test uses a stand-in connector. | FAKES | `auto_ontology/catalog/tests/test_table_filter.py` | 2026-11-10 |
| Evaluation harness | `dev_tools/evals` scores a running backend by comparing executed rows, and compares two reports question by question. The scoring and comparison logic is unit-tested; the harness has been run against the running stack. | REAL-E2E | `dev_tools/evals/tests/`, eval run labelled `candidate-junction+projection` | 2026-11-10 |
| Chinook held-out set | 20 questions on a second database. Expected rows were computed from the real SQLite database. The engine has NEVER been run on this set, so it supports no engine claim yet. The 20 expected answers have not been reviewed by a human. | UNVERIFIED | `dev_tools/evals/cases/chinook.json`, PR #19 | never |
| Evaluation baselines | The first Pagila baseline (79.5%) was measured while the semantic index was EMPTY, so it measured the raw schema, not the ontology. Later numbers (61.5% and 94.9%) were measured with the index populated. Never compare against the first baseline. Since the pre-flight check, a run refuses an empty index and a report records the index state. | REAL-E2E | `docs/evaluation.md`, eval runs labelled `main` and `candidate-junction+projection` (reports not committed) | 2026-11-10 |
| Value linking | Question values are matched to stored values (exact, built-in ISO aliases, then embedding and character similarity) from an opt-in value index. | FAKES | PR #25, `auto_ontology/retrieval/text_to_sql/tests/test_value_linking.py`; the 14 Pagila cases are a development set, the 14 Chinook cases are held-out and have never been run | 2026-11-11 |
| Clarification questions, backend | The agent asks what an unknown term means instead of guessing, behind the `clarification_enabled` flag (off by default). | FAKES | PR #26, `auto_ontology/retrieval/text_to_sql/tests/test_clarification.py`; no real model or embedding was run, the distance thresholds are uncalibrated, the VIP case was not reproduced | 2026-11-11 |
| Clarification questions, interface | The chat card for a clarification question. | FAKES | PR #27, `frontend/lib/clarification.test.ts`; parser and markup only (static render, no DOM), never streamed against the backend and never opened in a browser | 2026-11-11 |
| Internal API authentication | A token between the web app and the API. | UNVERIFIED | in progress | never |
| Settings interface for the above | Row limit field, semantic index status and a rebuild button in Settings. | FAKES | PR #28, `frontend/lib/semanticIndex.test.ts`, `frontend/lib/sqlResultMaxRows.test.ts`; pure logic only, no browser session, the rebuild button was never pressed against a real backend | 2026-11-11 |
| Business metrics through SQL attributes | A defined business term such as "VIP customer" is used by the chat through a SQL attribute. The test was not run. | UNVERIFIED | none | never |
| Interface redesign | Glass design, sidebar with search, light/dark switcher, page redesigns, assistant-ui chat thread, folded reasoning. Rendered on the running stack (README screenshots and demo). No automated interface test covers the visuals. | REAL-E2E | `docs/assets/screenshots/demo.gif`, PR #7 | 2026-11-10 |
| Official base images | The images build on Docker Official bases. CI builds the image but does not run it, and no runtime check is recorded for the rebuilt images. | UNVERIFIED | `.github/workflows/ci-python.yml` (job `build_image`, build only) | never |
| Release workflow | `release.yml` publishes images and the Helm chart on `v*` tags. No tag has been pushed, so it has never run. | UNVERIFIED | `.github/workflows/release.yml` | never |
| Ingestion and querying on the other connectors | MySQL, ClickHouse, Snowflake, Databricks, Kyuubi, HeavyDB, DuckDB, Trino ingestion and metadata queries are covered by unit tests with fake drivers, with no live engine behind them. | FAKES | `auto_ontology/connectors/tests/test_kyuubi.py`, `auto_ontology/connectors/tests/test_snowflake.py`, `auto_ontology/connectors/tests/test_databricks.py` | 2026-11-10 |
| MCP server, REST API and agent skills | Inherited from upstream. This fork did not re-verify them end to end. | UNVERIFIED | `mcp/tests` | never |
