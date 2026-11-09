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

## Planned: foreign keys on partitioned tables

On Postgres, foreign keys declared on a partitioned table's partitions (e.g.
Pagila's `payment_p2022_*`) are lost when partitions are folded into their
parent, so `payment` shows up without its links to `rental`, `customer` and
`staff`. The fix will go in the Postgres connector.
