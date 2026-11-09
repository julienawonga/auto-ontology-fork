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

| Feature | Branch | Status |
|---|---|---|
| [Any OpenAI-compatible model provider](#any-openai-compatible-model-provider) | `neutral-llm-typing` | Done, verified end to end |
| [Exclude views from ingestion](#exclude-views-from-ingestion) | `exclude-views-filter` | Done, verified end to end |
| [Collapsible reasoning and SQL in chat](#collapsible-reasoning-and-sql-in-chat) | `collapsible-sql` | Done, verified in the UI |
| [Chat-app style thread (assistant-ui)](#chat-app-style-thread-assistant-ui) | `chat-assistant-ui` | Done, not yet tested in the UI |
| [Glass design on the Chat page](#glass-design-on-the-chat-page) | `chat-glass` | Done, verified in the UI |
| [Foreign keys on partitioned tables](#planned-foreign-keys-on-partitioned-tables) | — | Planned |

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

## Glass design on the Chat page

A first step away from the upstream look: the Chat page uses a frosted-glass
style (translucent panels, background blur, light borders) over a softly
tinted page, with its own palette and typefaces. Other pages are unchanged.

What changed:

- Palette as tokens in `frontend/app/globals.css` (`glass-primary` #1856FF,
  `glass-secondary`, `glass-success`, `glass-canvas`, …), used through Tailwind
  classes such as `bg-glass-primary`. Blue replaces the green accent on the
  Chat page.
- Plus Jakarta Sans for text and JetBrains Mono for SQL, loaded by
  `frontend/app/chat/layout.tsx` for the Chat page only.
- One recipe for the glass surface, hover, focus ring and primary button in
  `frontend/components/chatPage/glass.ts`, applied to the composer, the
  question bubble, the conversation list, the scroll button and result cards.
- A single-value result (such as a total) shows as a card with a large figure.
- Visible keyboard focus on every control; text keeps the app's dark tones so
  it stays readable on the frosted surface.

Not yet covered: the active conversation highlight and the left navigation
still use the green accent, as they are shared with other pages.

## Planned: foreign keys on partitioned tables

On Postgres, foreign keys declared on a partitioned table's partitions (e.g.
Pagila's `payment_p2022_*`) are lost when partitions are folded into their
parent, so `payment` shows up without its links to `rental`, `customer` and
`staff`. The fix will go in the Postgres connector.
