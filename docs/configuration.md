<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Configuration

Lexis is configured with environment variables, read from `.env` by Docker
Compose. [`.env.example`](../.env.example) documents every variable; this page explains
the ones you set first: the models.

## Model providers

Lexis calls the model endpoints you configure for inference, embeddings and
reranking. Any OpenAI-compatible endpoint works: a gateway such as a LiteLLM proxy, a
hosted provider, or a local server (vLLM, Ollama, LM Studio). For OpenAI models behind a
gateway that exposes them as `openai/<model>`, set:

```dotenv
DEFAULT_MODELS_API_KEY=<gateway key>
DEFAULT_MODELS_ENDPOINT=http://localhost:4000/v1
DEFAULT_MODELS_MODEL=openai/gpt-4.1
EMBED_MODEL=<an embedding model>
EMBED_DIMENSIONS=<its output width, e.g. 1536>
EMBED_EXTRA_FIELDS=false
```

With an `openai/`, `azure/` or `aws/` prefix the model's default temperature is used;
other model names get an explicit temperature. The gateway must map the name to the real
model.

### Embedding width

The vector store is 2048 wide by default. For an embedding model with another width, set
`EMBED_DIMENSIONS` to it (it is also sent to the model as `dimensions`) and re-ingest; a
vector of the wrong width fails with an error that names this setting.

Embedding requests carry three extra body fields by default (`input_type`, `truncate`,
`modality`) that some embedding servers use to tell queries from passages. Set
`EMBED_EXTRA_FIELDS=false` if your endpoint rejects unknown fields, as most OpenAI-compatible
gateways do (the older name `EMBED_NIM_FIELDS` is still accepted).

## Model triplets

Each model role is configured by a **triplet** of environment variables:
`<PREFIX>_ENDPOINT` (URL), `<PREFIX>_API_KEY`, and `<PREFIX>_MODEL`.

| Prefix           | Role                                                        |
| ---------------- | ----------------------------------------------------------- |
| `DEFAULT_MODELS` | Shared default; every field below falls back to it.         |
| `REASONING`      | Main chat / NL-to-SQL model.                                |
| `NON_REASONING`  | Lighter model used for entity extraction.                   |
| `EMBED`          | Text-embedding model (must match between ingest and query). |
| `RERANK`         | Reranker used by the retrieval flow.                        |

You can set a full triplet, only some of its fields, or none at all: any field left unset
falls back to the matching `DEFAULT_MODELS_<FIELD>`. The simplest setup fills in
`DEFAULT_MODELS_*` and overrides only what differs (e.g. `EMBED_MODEL`,
`NON_REASONING_MODEL`).

## Model roles

| Role            | What it does                                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `REASONING`     | Builds the ontology (terms, relationships, SQL attributes), writes each answer's SQL and explanation, and grades answerability |
| `NON_REASONING` | Runs the agent's faster steps: question intent, entity extraction, candidate selection, and SQL checks and repair              |
| `EMBED`         | Indexes tables, columns, terms, and SQL attributes in pgvector, and matches each question to them                              |
| `RERANK`        | Optional. Ranks candidate tables when compilation builds bridge-table SQL attributes; on failure the first candidate is used   |

Every chat model is reached through the OpenAI client against its configured endpoint.
If a model field is missing, the server stops with an error that names the variables to
set; there are no built-in vendor defaults. Use models that support structured output, and keep the embedding model the same for ingestion and
queries.

## Predictions

Set `KUMO_RFM_API_KEY` and the agent routes predictive questions to KumoRFM. KumoRFM is a
hosted service: to answer, the agent uploads the connected tables to it, capped by
`KUMO_MAX_TABLES` (20 by default) and `KUMO_MAX_ROWS_PER_TABLE` (unlimited unless set).
Review what that sends, and complete your organization's data review, before you enable
it.

## Sources

Add connections under **Settings → Connections**, or list them in `CONNECTION_STRINGS`
(connections created that way cannot carry options such as **Exclude Views**). DuckDB and
SQLite are connection-string only. See [connections.md](./connections.md) for URL schemes,
scoping, and credential storage.

Each source costs its database no connections while idle, and queries wait for a free
connection instead of failing when many arrive at once. The limits are
`SOURCE_POOL_MAX_SIZE`, `SOURCE_POOL_MAX_IDLE_S`, `SOURCE_MAX_CONNECTIONS` and
`SOURCE_CONNECTION_WAIT_S`; see [source-connections.md](./source-connections.md) for
defaults and how to size them against a source's `max_connections`.

## Chat concurrency

Every conversation runs in its own agent subprocess — a full Python process that imports
the agent and opens its own database pools. `CHAT_MAX_CONCURRENT` (default 4) bounds how
many conversations run at once **per backend process**: further requests wait up to
`CHAT_WAIT_S` (default 120) for a free slot and are then refused with 503 and a
`Retry-After` header instead of cold-starting one more subprocess. The single warm
standby subprocess is extra, so at most `CHAT_MAX_CONCURRENT + 1` worker subprocesses
exist in steady state (briefly one more while a cancelled worker is being killed and its
replacement starts). Size it against the host's memory: each worker is roughly one copy
of the backend's Python footprint. `GET /api/health` reports the current
`chat_workers.in_use` and `chat_workers.cap`.

## Startup ingestion

The ingestion service runs one ingest pass when it starts, then one every 24h. The
startup pass ingests only the databases whose catalog and embeddings are not already
stored — a database whose tables and vectors are present is skipped, because re-ingesting
it would re-embed every row through the embedding endpoint (a paid request per row) to
reproduce what is already in the store. A restart no longer re-embeds the whole
deployment.

The skip is not a source diff: the service cannot tell whether a source changed while it
was down without extracting it, which is the expensive half of an ingest. Changes are
picked up by the mechanisms that already exist for that:

- the scheduled 24h pass re-ingests every connection in full;
- `POST /ingest` on the ingestion service (port 3002) re-ingests one connection in full;
- editing a connection's schema allowlist or table filters in the UI re-ingests it.

Set `INGEST_ON_STARTUP=all` to restore the previous behaviour and re-ingest every
connection at every startup. A database is only skipped when *both* its catalog tables
and its data-layer embeddings are present, so a catalog left half-ingested by a failed
embed is re-ingested, and an unreachable store counts as "not ingested".
