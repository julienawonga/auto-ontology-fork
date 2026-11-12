<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Deploying Lexis on Kubernetes

## Released artifacts

Pushing a `vX.Y.Z` tag runs
[`.github/workflows/release.yml`](.github/workflows/release.yml), which builds
the images for `linux/amd64` and `linux/arm64` and publishes them, with the Helm
chart, to the GitHub Container Registry:

| Artifact | Location |
|---|---|
| Backend + ingestion image (also runs the Alembic migrate Job) | `ghcr.io/julienawonga/auto-ontology` |
| Frontend (Next.js) image (also runs the Prisma migrate Job) | `ghcr.io/julienawonga/auto-ontology-frontend` |
| MCP server image | `ghcr.io/julienawonga/auto-ontology-mcp` |
| Helm chart | `oci://ghcr.io/julienawonga/charts/auto-ontology` |

A release tagged `v1.2.3` tags each image `1.2.3`, `1.2` and `sha-<commit>`,
and publishes chart version `1.2.3`, whose images are pinned to `1.2.3`. A
manual run of the workflow (`workflow_dispatch`) publishes `sha-<commit>`
images only, without a chart.

The workflow authenticates with the repository's `GITHUB_TOKEN`; no secret is
needed. GHCR creates new packages as private: after the first release, make
each package public (package settings → Change visibility) or give the cluster
a pull secret through the chart's `imagePullSecrets` value.

The MCP server is published as an image but is not part of the chart; deploy
it separately (see [`docs/mcp.md`](docs/mcp.md)) and set `mcpPublicUrl` to its
public origin.

To build or push the images by hand, see `make help`
(`make publish REGISTRY=ghcr.io/<owner>` after `docker login ghcr.io`).

## Deploying a Release

1. Pick a chart version from the repository's
   [packages](https://github.com/julienawonga/lexis/packages) or
   tags.

2. Put the secrets and model settings in a values file. Create it in an editor
   rather than on the command line, so they stay out of shell history and
   process arguments, restrict it with `chmod 600`, and keep it out of git:

   ```yaml
   # auto-ontology-secrets.yaml
   defaultModelsApiKey: <API-KEY>
   defaultModelsEndpoint: <OPENAI-COMPATIBLE-ENDPOINT>   # e.g. https://gateway.example.com/v1
   defaultModelsModel: <CHAT-MODEL>
   embedModel: <EMBEDDING-MODEL>
   embedDimensions: "<EMBEDDING-WIDTH>"                  # e.g. "1536"; empty = 2048
   rerankModel: <RERANK-MODEL>
   postgresPassword: <POSTGRES-PASSWORD>
   adminEmail: <ADMIN-EMAIL>
   adminPassword: <ADMIN-PASSWORD>
   internalApiToken: <INTERNAL-API-TOKEN>    # optional; generated and kept across upgrades when empty
   connectionStrings: <CONNECTION-STRINGS>   # optional; or add connections in the UI
   ```

   Lexis has no built-in model provider: point it at any
   OpenAI-compatible endpoint (a gateway such as a LiteLLM proxy, a hosted
   provider, or a self-hosted server). The `defaultModels*` triplet is shared
   by every model role, and the per-role values in `values.yaml`
   (`reasoningModel`, `nonReasoningModel`, `embed*`, `rerank*`) override single
   fields. [`docs/configuration.md`](docs/configuration.md) explains each
   setting; the chart passes them to the pods as the environment variables
   documented there.

3. Install the chart:

   ```bash
   helm install auto-ontology oci://ghcr.io/julienawonga/charts/auto-ontology \
     --version <version> -f auto-ontology-secrets.yaml
   ```

   `helm list` shows which chart version was installed.

   `adminEmail` and `adminPassword` are required: self-service sign-up is
   disabled, so they create the first admin, and the install fails without
   them. If users will reach the app somewhere other than
   `http://localhost:3000` (the port-forward below), also set `appUrl` to that
   exact origin.

   `internalApiToken` is the shared secret the web app presents to the API and
   the ingestion service, which refuse requests without it. The chart generates
   one on first install and preserves it across `helm upgrade`; set it
   explicitly if you render with `helm template` or a GitOps tool without
   cluster access, or the value changes on every render. See
   [SECURITY.md](SECURITY.md#internal-api-token). The pods restart together on
   upgrade, so expect a few failed requests while the old and new pods overlap.

4. Expose the UI:

   ```bash
   kubectl port-forward svc/frontend 3000:3000
   ```

## Installing from a checkout

`helm install auto-ontology ./helm/auto-ontology -f auto-ontology-secrets.yaml`
installs the chart from source. Its `values.yaml` names the images of the last
release; to deploy images you built yourself, override `backend.image`,
`ingestion.image` and `frontend.image` (`repository` and `tag`, or `digest`).
