<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Lexis in a broader stack

Honest map of what this repository implements versus what a partner wires.
Do not invent a first-party blueprint that is not here.

## Agents that forward an SSO token (auth contract)

Lexis normally authenticates browsers with a Better Auth cookie. An agent
or app that already signs users in through the same SSO provider can forward the
**user's SSO id token** as `Authorization: Bearer <jwt>`. Lexis verifies
signature, issuer, and expiry against the configured SSO provider's JWKS — the
same issuer as interactive SSO. Audience is **not** checked: the token was minted
for the calling app's OAuth client, so `aud` is that app's client id, not Auto
Ontology's. Trusting any token that SSO provider signed is the intended model. No
shared service secret.

The bearer is mapped to an Lexis user by email, then by SSO account subject.
There must already be a matching Lexis user.

This is an auth contract, not instructions for building the calling agent.

Credential order on every `/api` route (`frontend/auth/resolve-user.ts`):

1. Session cookie
2. Lexis API token (`auto_ontology_…`, `x-api-key` or Bearer)
3. OAuth bearer issued by Lexis (MCP)
4. SSO id token (forwarded by a trusted caller)

A revoked API token is a 401; it is not retried as an SSO JWT.

## Model endpoints (deployment config)

Lexis calls OpenAI-compatible model endpoints for chat, embeddings, and
rerank. That is `.env` / Helm values, not a model training or fine-tuning
integration.

Triplets: `<PREFIX>_ENDPOINT`, `<PREFIX>_API_KEY`, `<PREFIX>_MODEL`. Unset
fields fall back to `DEFAULT_MODELS_*`. Prefixes: `DEFAULT_MODELS`,
`REASONING` (NL-to-SQL), `NON_REASONING` (entity extraction), `EMBED`,
`RERANK`.

`EMBED_ENDPOINT` + `EMBED_MODEL` must be the same at ingest and query.
Changing the embedding model without re-ingest breaks retrieval.

## Optimization solvers (partner-wired)

This repository has **no** optimization-solver client, skill, or job bridge. Auto
Ontology returns natural-language answers, the SQL it ran, and rows. A partner
agent may take those numbers and pass them to a solver. Do not claim Auto
Ontology schedules routes or solves LPs itself.

## Calling rules that bite in a stack

- Partners call the Next.js gateway (`APP_URL` / `:3000`), never public
  FastAPI `:3001`.
- MCP holds no deployment-wide identity. Each caller signs in as themselves.
- `conversation_id` is per Lexis user. Do not share UUIDs across users.
- Overlapping turns on one conversation: `409`.
