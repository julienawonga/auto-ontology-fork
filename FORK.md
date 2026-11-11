<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# What this fork adds

This repository is a fork of
[NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) (Apache-2.0).
Upstream does not take external pull requests, so the changes below live here.
Each one is opt-in or backwards compatible: with no new settings, the fork
behaves like upstream.

Every feature below is merged into `main`.

| Feature | Area |
|---|---|
| [Any OpenAI-compatible model provider](#any-openai-compatible-model-provider) | Backend |
| [Exclude views from ingestion](#exclude-views-from-ingestion) | Backend, connections |
| [Official base images](#official-base-images) | Containers, CI |
| [Glass design across the app](#glass-design-across-the-app) | Frontend |
| [Sidebar with search and account](#sidebar-with-search-and-account) | Frontend |
| [Light/dark theme switcher](#lightdark-theme-switcher) | Frontend |
| [Blue accent and neutral logo](#blue-accent-and-neutral-logo) | Frontend |
| [Chat-app style thread (assistant-ui)](#chat-app-style-thread-assistant-ui) | Chat |
| [Collapsible reasoning and SQL in chat](#collapsible-reasoning-and-sql-in-chat) | Chat |
| [Clarification questions](#clarification-questions) | Chat, backend |
| [Page redesigns](#page-redesigns) | Terms, Analysis, Analytics, Discovery, Exploration |
| [Foreign keys on partitioned tables](#planned-foreign-keys-on-partitioned-tables) | Planned |

## Any OpenAI-compatible model provider

Upstream assumes NVIDIA-hosted (NIM) models: the vector store is fixed at 2048
dimensions, and every embedding request carries NVIDIA-only body fields. An
embedding model with another width, or a gateway that rejects unknown fields,
breaks retrieval without any clear error. The chat then answers "SQL can't be
constructed" because no vectors were ever stored.

What changed:

- **`EMBED_DIMENSIONS`**: sets the width of the stored vectors (default `2048`)
  and is sent to the model as `dimensions`. The pgvector tables are created at
  this width (`auto_ontology/vdb/postgres.py`).
- **`EMBED_NIM_FIELDS`**: set it to `false` to drop `input_type`, `truncate` and
  `modality` from embedding requests (default `true`).
- **Clear error on width mismatch**: if the model returns vectors of a
  different width, ingestion fails with a message naming `EMBED_DIMENSIONS`,
  instead of failing later inside pgvector.
- **Provider-neutral typing**: the text-to-SQL agent state accepts any
  LangChain `BaseChatModel`, not only `ChatNVIDIA`.
- **Provider-neutral docs**: `README.md` and `.env.example` describe a generic
  OpenAI-compatible endpoint (e.g. a LiteLLM gateway).

Example for a 768-dimension embedder behind a gateway:

```env
EMBED_MODEL=text-embedding-005
EMBED_DIMENSIONS=768
EMBED_NIM_FIELDS=false
```

Changing `EMBED_DIMENSIONS` on an existing install requires recreating the
vector tables (e.g. `docker compose down -v`) and ingesting again.

Code: `auto_ontology/utils/model_config.py`, `auto_ontology/utils/embedding.py`,
`auto_ontology/retrieval/text_to_sql/state.py`. Tests: `test_model_config.py`,
`test_embedding.py`.

## Exclude views from ingestion

Databases often expose views that only re-present tables that are already
catalogued (Pagila has seven). Each view becomes a second, near-identical Term,
which clutters the ontology and lets the agent take shortcuts through a view
instead of joining the real tables.

What changed:

- New connection option **`exclude_views`**: skips every view and materialized
  view **by type**, not by name, so no regex is needed.
- It combines with `table_allow_regex` / `table_deny_regex`, and changing it
  triggers re-ingestion, just as changing those does.
- It works with any connector: it uses the relation type when the connector
  reports one, and otherwise the connector's list of views.
- **UI**: an "Exclude Views" checkbox in *Settings > Connections*.

Verified on Pagila: 15 base tables catalogued, all 7 views skipped.

Code: `auto_ontology/catalog/table_filter.py`, `frontend/enums/connection.ts`,
`frontend/types/connection.ts`. Docs: `docs/connections.md`. Tests:
`test_table_filter.py`.

Limitation: when `CONNECTION_STRINGS` is set in `.env`, the connection created
at startup cannot carry this option. To use it, create the connection from the
UI instead.

## Collapsible reasoning and SQL in chat

A chat answer shows its explanation and the SQL it ran in full, which pushes
the result itself down the page. Business users mostly want the number;
checking how it was computed should stay one click away.

What changed:

- An answer that carries SQL folds its explanation and SQL behind
  "Show reasoning and SQL". The results and charts always stay visible, and
  replies without SQL (including errors) are never folded.
- New admin setting **Expand Reasoning and SQL** in *Settings > Agent
  Settings*, stored as `chat_details_expanded` in the `configurations` table.
  Off (the default, also when the row is missing): answers start collapsed.
  On: they start expanded. Each answer can still be toggled by hand.
- Route `/api/configurations/chat-details`: anyone who can chat may read it,
  and changing it requires the same permission as "Visualize SQL Results".

Code: `frontend/components/chatPage/MessageBubble.tsx`,
`frontend/components/chatPage/MessageList.tsx`,
`frontend/app/settings/agent-settings/page.tsx`,
`frontend/app/api/configurations/chat-details/`,
`frontend/lib/queries/chatDisplay.ts`.

## Chat-app style thread (assistant-ui)

The chat input was a separate full-width bar, and answers sat in grey
bubbles. The thread now follows the familiar chat-app layout, and its input
behaviour comes from a maintained library instead of code kept in this repo.

What changed:

- The thread is built on the unstyled primitives of
  [assistant-ui](https://github.com/assistant-ui/assistant-ui)
  (`@assistant-ui/react`, pinned to 0.15.31), styled with the app's own
  Tailwind classes. No shadcn/ui setup is added.
- From the library: the auto-growing composer (Enter to send, Shift+Enter for a
  new line), a **Copy** button under each answer, clickable **suggestions**,
  the Stop button while an answer runs, and stick-to-bottom scrolling with a
  "scroll to latest" button.
- Suggestions are the names of the saved SQL analyses (Analysis page), so they
  follow that page instead of a hand-kept list.
- Questions sit in a bubble on the right; answers read as plain text across
  the column. The SQL block, result tables, charts, live progress steps and the
  collapsible reasoning are unchanged.
- The stream to the backend is untouched: `useChat` still owns the messages,
  and assistant-ui reads them through its external-store runtime.

Code: `frontend/components/chatPage/ChatThread.tsx`,
`frontend/lib/queries/chatSuggestions.ts`,
`frontend/components/chatPage/MessageBubble.tsx`. It replaces
`ChatInput.tsx` and `MessageList.tsx`.

## Glass design across the app

Every page uses a frosted-glass style: translucent panels with background
blur and light borders, over a softly tinted page with two colour fields.

What changed:

- **Surfaces**: two utilities in `frontend/app/globals.css`, `glass` for cards
  and panels and `glass-strong` for menus, dialogs and side panels that float
  over content. Page backgrounds are transparent so the tinted ground shows.
- **Typefaces**: Plus Jakarta Sans for text and JetBrains Mono for code, loaded
  in `frontend/app/layout.tsx` and mapped to Tailwind's `font-sans` and
  `font-mono`.
- **Controls**: rounder buttons (primary in bold), translucent inputs and select
  fields, pill-shaped tabs, 28px page titles and a 20px card radius
  (`rounded-card`).
- **Code blocks** (SQL, raw results, copyable values) follow the theme: light
  and translucent in light mode, dark in dark mode, with SQL keywords, strings,
  numbers and comments highlighted (`frontend/lib/sqlHighlight.ts`).
- **Sign-in, invite and OAuth pages** show their form in a glass card.
- Visible keyboard focus on every control; text keeps the app's dark tones so
  it stays readable on the frosted surface.

The Chat page keeps its own recipe in `frontend/components/chatPage/glass.ts`.
The Exploration graph keeps an opaque canvas, because its labels are drawn
against that exact colour.

## Sidebar with search and account

Upstream splits navigation between a top bar (logo, breadcrumbs, search,
account) and an icon rail that opens over the page on hover.

What changed:

- The top bar is gone; the sidebar holds the logo, a **Search…** field (also
  opened with **Ctrl+K**, or **⌘K** on a Mac), the navigation, and the signed-in
  **account at the bottom**, whose menu opens upward.
- A toggle at the top switches the sidebar between **open** (icons and labels)
  and **collapsed** (icons only). The choice is stored per browser.
- Breadcrumbs show in a slim line above the content, only when a page goes
  below its section (a database, a term, a tag).

Code: `frontend/common/NavRail.tsx`, `frontend/common/UserMenu.tsx`,
`frontend/common/PageBar.tsx`, `frontend/lib/navRail.ts`.

## Page redesigns

- **Terms**: compact cards in two columns and a side panel for the selected
  term (description, first column attributes, counts, tags), with **Open term**
  for the full page. Double-click or Enter opens a term directly.
- **Analysis**: cards in two columns, folded to the title and one line of
  description; the full description and SQL open on demand.
- **Analytics**: each question asked in chat is a card with its asker, source,
  time and answer time, and its reasoning and SQL folded behind a toggle,
  instead of a six-column table with truncated text.
- **Discovery**: the search sits in a glass bar, and results are cards with a
  coloured type badge.
- **Exploration**: the legend lists only the node kinds on the graph, with
  their counts, and updates as nodes are expanded or collapsed. Terms are blue
  and column attributes gold in the graph.

## Light/dark theme switcher

Upstream only follows the operating system's light/dark setting, with no way
to override it in the app.

What changed:

- **Theme choice in the account menu**: a System / Light / Dark control in the
  menu of the account at the bottom of the sidebar. The default is System.
- **Stored per browser** (`localStorage`), not per account: no database or API
  change. If storage is blocked, the app falls back to System.
- **No flash on load**: a small inline script in the root layout sets the
  theme before the first paint.
- **Live updates**: while on System, the app follows OS changes as they happen,
  and a change made in another tab carries over.
- Tailwind's `dark:` variant now keys off a `dark` class on `<html>` instead of
  the OS media query. The few hand-written dark CSS rules and the exploration
  graph's canvas colors follow the same class.

Code: `frontend/lib/theme.ts`, `frontend/common/ThemeSwitcher.tsx`,
`frontend/enums/theme.ts`, `frontend/app/layout.tsx`,
`frontend/app/globals.css`, `frontend/common/UserMenu.tsx`,
`frontend/components/explorationPage/graph/GraphCanvas.tsx`.

## Blue accent and neutral logo

The upstream green accent and the NVIDIA logo are replaced everywhere, so the
fork no longer carries NVIDIA's branding in its interface. The copyright and
license notices are unchanged, as Apache-2.0 requires.

What changed:

- One accent token family in `frontend/app/globals.css`: `primary` (#1856FF),
  `primary-hover`, `primary-strong`, `primary-soft`, `primary-subtle`, with
  lighter values in dark mode so text stays readable. Changing the accent of the
  whole app is now a one-line edit.
- Every hard-coded green (`#76b900` and its lighter and darker shades, about
  250 uses in 63 files) now uses those tokens.
- A neutral logo (a magnifier framing a node, for Lexis) replaces the NVIDIA logo in the sidebar,
  the chat, the login and invite pages, the browser tab icon and the MCP
  server's icon.

Code: `frontend/app/globals.css`, `frontend/common/icons/svg/app-logo.svg`,
`frontend/public/favicon.svg`, and the class names across `frontend/`.

## Official base images

The upstream images build on NVIDIA's Ubuntu 22.04 base from nvcr.io, pinned to
June 2025, and install Node.js and uv with scripts piped from the internet.

What changed:

- Backend, ingestion and MCP images build on `debian:bookworm-slim`; the
  frontend image on `node:22-bookworm-slim`. Both are Docker Official Images,
  pinned by digest.
- uv is copied from Astral's official image (`ghcr.io/astral-sh/uv`) and
  Node.js comes with its base image, so no install script is piped to a shell.
- Security updates are still applied at build time (`apt-get upgrade`).
- The CI image builds no longer log in to nvcr.io, and the NVIDIA-only publish
  and release workflows are disabled on this fork.
- `CONTAINER_THIRD_PARTY_NOTICES.md` lists the new bases and packages.

Code: `Dockerfile`, `frontend/Dockerfile`, `mcp/Dockerfile`,
`.github/workflows/ci-python.yml`, `.github/workflows/ci-client.yml`.

## Vendor-neutral by default

No model, registry or credential is tied to a single vendor any more.

- **Models:** no built-in defaults. A missing model setting fails with an error naming the environment variables to set
  (`DEFAULT_MODELS_*` or the per-role ones). `NVIDIA_API_KEY` is no longer read; `EMBED_EXTRA_FIELDS` replaces
  `EMBED_NIM_FIELDS` (the old name is still accepted). All chat goes through `ChatOpenAI`.
- **Vault:** `VAULT_AUTH_MOUNT` defaults to `approle`.
- **Skills:** renamed `skills/ontology-*`; the vendor signatures were removed.
- **Releases:** one `release.yml` publishes `ghcr.io/<owner>/auto-ontology`, `-frontend` and `-mcp` (plus the OCI Helm
  chart) on `v*` tags. The NGC workflows and `release_helm_chart.py` are gone. The chart's image tag has no image on GHCR
  until the first `v*` tag is pushed.
- **Docs and notices:** README, SECURITY, CONTRIBUTING, CODEOWNERS and the third-party notices point to this fork.

## Read-only, row-capped agent SQL

The chat runs SQL written by a model. Upstream only parsed it against the catalog, so a write was limited only by the
database user's grants. Details: [docs/query-safety.md](./docs/query-safety.md).

- **Parse-time guard:** one read-only `SELECT` only; a write nested in a CTE, `SELECT INTO`, row locks and several
  statements are refused, and unparseable SQL is refused.
- **Enforced by the database** on PostgreSQL, MySQL/MariaDB, SQLite, DuckDB, ClickHouse and Trino (`supports_read_only_execution`).
  Snowflake, Databricks, Kyuubi and HeavyDB rely on the guard alone.
- **Row cap:** `sql_result_max_rows` (10,000 by default). A cut result is disclosed in the answer and in
  `result_truncated_at`. PostgreSQL stops reading at the cap through a server-side cursor.

Code: `auto_ontology/retrieval/text_to_sql/sql_guard.py`, `chat_sql.py`, `auto_ontology/connectors/*.py`.

## Verification register and integrity checks

[docs/verification-status.md](./docs/verification-status.md) lists every capability above with the level it was actually
verified at (`REAL-E2E`, `REAL-ENGINE`, `FAKES` or `UNVERIFIED`) and the evidence. `python -m dev_tools.integrity`, run in
CI, fails when the shipped code contains an eval question or answer, when a frozen case file was edited, or when a
register row is malformed. The eval harness refuses an empty semantic index and refuses to compare reports taken under
different conditions.

## Evaluation harness

`dev_tools/evals` scores a running backend against questions with known answers by comparing the rows the generated SQL
returns, and compares two runs question by question. Engine changes are accepted or rejected on that result.
Details and the rules for an honest measurement: [docs/evaluation.md](./docs/evaluation.md).

## Semantic layer embedded with the configured parameters

The semantic embedder built its request parameters by hand and ignored `EMBED_EXTRA_FIELDS` and `EMBED_DIMENSIONS`. On an
endpoint that rejects the extra fields every Term and ColumnAttribute was refused, the semantic index stayed empty, and
the chat answered from the raw schema while compilation reported success. It now uses the same parameters as every other
embedder.

## Detecting and repairing the semantic index

Nothing used to notice an empty index, and nothing could rebuild it: compilation only embeds a table's Terms once, when it
is first compiled.

- **`GET /api/semantic-compilation/status`** reports, per database, the Terms and ColumnAttributes `expected` from the
  relational store, how many are `indexed`, and how many are `missing` (matched by id, not by count).
- **`POST /api/semantic-compilation/reindex?database_name=...`** (permission `semanticCompilation: manage`) rebuilds that
  database's index from the stored Terms and returns `expected` and `written`. It replaces the rows it rewrites, so it can be
  repeated. 404 for an unknown database, 409 while a compilation runs, 503 when embedding is not configured.
- **A compilation** that leaves Terms or ColumnAttributes out of the index is recorded as failed, no longer as a success.
- **Chat** logs a warning when retrieval finds nothing for a database whose index holds none of its Terms, instead of silently
  answering from the raw schema.

On Settings > Semantic Compilation, each database shows its index status (complete, or "N of M missing", in a warning
style) with a **Rebuild index** button that calls the reindex route. The button needs the `manage` permission and is
disabled while a compilation runs; a partial result, a 409 (compilation running), a 503 (embedding not configured) and a
404 are reported in plain text.

Run the reindex after any ingestion that was made while the embedding endpoint was failing, and whenever `missing` is not 0.

## Matching question wording to stored values

A question that does not spell a value the way the data does ("USA" for `United States`, "Indai" for `India`) filtered on
a literal that does not exist and answered 0. The chat already consumed `value_anchors` but nothing produced them.

- **`POST /api/semantic-compilation/value-index?database_name=...`** (permission `semanticCompilation: manage`) copies the
  distinct values of small text columns into the semantic index. Opt-in, never run by ingestion. It excludes primary keys,
  columns tagged `PII`, free text and columns with more than 2,000 distinct values, so names and emails never leave the
  source. Returns `expected`, `written`, `columns_indexed`, `columns_skipped`.
- **Chat** looks the question's phrases up (exact after normalisation, then embedding plus character similarity) and adds
  the matches as verified values. A database with no index, or any failure, changes nothing.
- **Aliases**: ISO 3166 countries (codes and common names) and US state postal codes are resolved deterministically, only to
  values that are stored, so "USA", "U.S." and "America" reach `United States` without trusting an embedding model.
- **Eval cases**: `pagila_values.json` is a development set (it motivated the feature); `chinook_values.json` is the frozen
  held-out set, with values that do not exist. Report them separately.

Abbreviations outside the alias table rely on the embedding model and were not measured. Details and thresholds: [docs/value-linking.md](./docs/value-linking.md).

Code: `auto_ontology/semantic/value_index.py`, `auto_ontology/retrieval/text_to_sql/value_linking.py`.

## Labels and ranking values in answers

- **Far side of a junction:** when the relevance filter keeps a junction table (`film_category`), the table on its other
  side (`category`, which holds the name) reaches SQL generation with its join. The chat used to return `category_id`.
- **Ranking answers** return the item and the value it is ranked by, and a name rather than a bare id. The strict
  benchmark mode (`shorten_answer`) keeps the upstream "item or value, never both" rule.

Code: `auto_ontology/dal/attributes.py` (`find_junction_far_side_tables`),
`auto_ontology/retrieval/text_to_sql/agents/candidates_preparation.py`, `auto_ontology/retrieval/text_to_sql/prompts.py`.

## Postgres keys on partitions and composite keys

Foreign and primary keys are read from `pg_constraint`. Keys declared on a partitioned table's partitions (Pagila's
`payment_p2022_*`) are reported for the parent, composite keys pair their columns by position instead of as a cross
product, two tables sharing a constraint name are no longer mixed, and foreign keys between two schemas are
no longer dropped (the old query matched on the referencing table's schema, which `constraint_column_usage` does not
report). Ordinary `INHERITS` children stay separate tables.
A source ingested before this change keeps its old keys until it is ingested again.

Code: `auto_ontology/connectors/postgres.py` (`get_fks`, `get_pks`).

## Clarification questions

Asked "How many VIP customers do we have?", the chat used to invent a definition ("spend above the average") and answer
confidently; with the real threshold the answer was different. With the feature on, it instead ends the turn with one
short question: 2 to 4 concrete readings of the term plus a free-text answer. The answer comes back on the next request
and the original question is answered with it as evidence.

- **Off by default.** Set the `clarification_enabled` configuration row to `true`; it is read per request. A client must
  also send `supports_clarification: true`, and the request needs a `conversation_id`, so the MCP server, scripts and the
  evaluation harness never receive a question.
- **Ask rarely.** At most one question per original question; none when evidence was supplied, when a verified definition
  (SqlAttribute, CustomAnalysis, Term) matches, when the term is a schema or value reference, or when the readings would
  not change the SQL. Any failure falls back to answering as before.
- **Stateless on the server.** The question is stored as an assistant message (a fenced `clarification` block, like charts)
  and is pending while it is the conversation's last message, so a stale, foreign or answered id is refused (422).
- The event and request contract is in [docs/conversation-api.md](docs/conversation-api.md#clarification-questions). The
  UI card is a separate change.
- Not built on `auto_ontology/retrieval/interactive`: that package is a multi-round benchmark loop with free-text questions
  and an external knowledge base, so only its threshold for "this term is covered" (0.62) was reused.

Code: `auto_ontology/retrieval/text_to_sql/clarification.py` (detector, option validation),
`auto_ontology/server/chat/clarification.py` (stored form, answer matching), `auto_ontology/server/chat/router.py`,
`auto_ontology/server/chat/worker.py`.

## Clarification questions in the chat

When the backend needs a business term defined ("How many VIP customers do we have?"), the chat shows a card instead
of guessing: a short question, 2 to 4 option buttons (each with an optional description) and an "Other" free-text row.
The backend only asks when `clarification_enabled` is on; it is off by default, so nothing changes until it is set.

- **Wire format:** the UI sends `supports_clarification: true` (and a `conversation_id`) on every request; only then can a
  `clarification` SSE event arrive (`clarification_id`, `term`, `question`, `options[]`, `allow_other`), after which the
  stream ends with `[DONE]`. The answer is the next request on the same conversation with
  `clarification: {clarification_id, option_id}` or `{clarification_id, text}`; `question` carries the same words and
  becomes the user message. A refusal (HTTP 422: stale, already answered, option not offered) closes the card and shows
  a message.
- **History:** the question is stored as an assistant message with a fenced `clarification` JSON block, parsed back into
  the card (`parseClarificationFence`). It is pending only while it is the last message; the user message after it
  is the answer, matched to an option label (otherwise shown as free text).
- **States:** pending (options enabled), answering (disabled, spinner on the chosen row), answered (compact, read-only),
  and stale (the conversation moved on without an answer: shown as "not answered", no buttons).
- **Keyboard:** the options are a radio group. Arrows, Home and End move between rows, Enter or Space picks one, and the
  card takes focus when it appears. In "Other", Enter sends, Shift+Enter adds a line, Escape goes back to the list;
  answers are trimmed, must not be empty, and are capped at 500 characters.
- **Without the backend:** `frontend/lib/clarification.fixture.ts` holds a canned event and its SSE framing; `pnpm test`
  (vitest) feeds it through the stream parser, the payload builder and the card's server-rendered markup.

Code: `frontend/components/chatPage/ClarificationCard.tsx`, `frontend/lib/clarification.ts`, `frontend/api/chat.ts`,
`frontend/lib/useChat.ts`.