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

## Rate limits and request caps

A gateway answers HTTP 429 (or 503) when it is asked for too much at once, and an edge
proxy in front of it may block a client that keeps hammering. Lexis protects the
gateway at the HTTP transport, so LLM, embedding and rerank requests all share it:

| Variable                       | Default | Meaning                                                                                      |
| ------------------------------ | ------- | -------------------------------------------------------------------------------------------- |
| `LLM_MAX_CONCURRENT_REQUESTS`  | `8`     | Max in-flight outbound requests per process (LLM, embedding and rerank together).            |
| `LLM_RATE_LIMIT_BUDGET_S`      | `60`    | Total seconds one request may wait on 429/503 before the error is raised. `0` never waits.   |
| `LLM_RATE_LIMIT_BASE_DELAY_S`  | `2`     | Ceiling of the first backoff; doubles per retry.                                             |
| `LLM_RATE_LIMIT_MAX_DELAY_S`   | `30`    | Longest single backoff.                                                                      |
| `EMBED_HTTP_CONCURRENCY`       | `4`     | Parallel embedding requests inside one embed call (was a fixed 32).                          |
| `LLM_REQUEST_SLOT_TIMEOUT_S`   | `300`   | Longest a request waits for a free slot before failing with an error naming the cap.         |

A wait for a slot longer than 5 s is logged at WARNING (saturation). An invalid value (not a number, or below the minimum) is ignored with a warning and the
default is used.

**Backoff.** On 429 or 503 the wait is a random time between 0 and
`min(max delay, base * 2^retry)` (full jitter), or exactly the `Retry-After` /
`Retry-After-Ms` the gateway sent. If the next wait would pass the budget (or after 8
retries), the original error is raised. One WARNING is logged per wait, with the delay.
The concurrency slot is released while waiting. Other errors (connection failures, 408,
409, 500, 502, 504) keep their previous handling.

**No nested retries.** The transport is the only layer that retries a 429/503. When its
budget is spent it marks the response so the OpenAI SDK does not retry it, the LLM and
embedding-query retry loops do not retry it, and a failed non-reasoning call does not fall
back to the reasoning model through the same gateway. Before this, one logical call could
make up to 18 HTTP attempts (SDK retries x application retries) within seconds; now it makes
at most 9 requests (the first plus 8 retries), spread over at most `LLM_RATE_LIMIT_BUDGET_S`
seconds of waiting. Ingest still retries a failed embedding chunk up to 4 times, 15 to 120
seconds apart, on top of that.

**Scope of the cap.** The cap is per process. Each chat conversation runs in its own worker
subprocess, so the effective global bound is processes x `LLM_MAX_CONCURRENT_REQUESTS`;
`CHAT_MAX_CONCURRENT` (when present in your version) bounds the number of processes. Model
requests keep httpx's environment handling (`HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`,
`SSL_CERT_FILE`, `SSL_CERT_DIR`). `LLM_MAX_INFLIGHT` (a per-block bound on LLM invokes) still
works and stacks with this cap.

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

Two more knobs bound how long a run may hold a slot:

- `CHAT_ORPHAN_GRACE_S` (default 60): how long a *conversation* run (one with a
  `conversation_id`) keeps going after its client disconnected and nobody reattached
  through `/chat/watch`; then it is cancelled (worker killed, slot freed) and the
  conversation records a failed turn. A stateless run (no `conversation_id`) cannot be
  reattached, so it is cancelled as soon as its client disconnects, and a request whose
  client leaves while still queued for a slot is dropped without taking one.
- `CHAT_MAX_RUN_S` (default 300): wall-clock limit for one question. A run over it is
  cancelled, the client receives an `error` event ("This question exceeded the
  CHAT_MAX_RUN_S limit ...") and the conversation records it as a failed turn.

Each cancellation is logged at WARNING with its reason (`client_disconnected`,
`orphaned`, `max_run_time`). The eval runner retries a 503 using `Retry-After` (up to 10
times / 15 minutes, recorded as `busy_retries` / `busy_wait_s` in the report meta) and
warns when `--workers` exceeds the cap reported by `GET /api/health`.

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
