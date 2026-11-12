<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Security policy

## Reporting a vulnerability

**Please do not report security vulnerabilities in public issues, discussions or pull
requests.**

Report them privately through GitHub instead: open the repository's **Security** tab and
choose **Report a vulnerability**
([direct link](https://github.com/julienawonga/lexis/security/advisories/new)).
Only the maintainers can see the report.

Please include:

- the version or commit you tested;
- the component involved (web app, API, ingestion service, MCP server, Helm chart…);
- steps to reproduce, and a proof of concept if you have one;
- the impact you expect, and how an attacker could use it.

You will get an acknowledgement within a few days. Once a fix is ready, it is released and
the advisory is published, crediting you unless you prefer otherwise.

## Supported versions

Security fixes go to the `main` branch. Run a recent `main` to get them.

## Deployment notes

- Keep ports 3001 (API) and 3002 (ingestion service) on a private network. The API trusts
  the user identity forwarded by the web app, and the ingestion service has no
  authentication of its own; clients go through the web app on port 3000. Docker Compose
  publishes the API, the ingestion service, PostgreSQL and pgAdmin on `127.0.0.1` only;
  `BIND_ADDRESS` in `.env` changes that, and exposing those ports lets anyone who can reach
  them bypass every permission the web app enforces.
- Set `INTERNAL_API_TOKEN` to protect those two ports with a shared secret; see
  [Internal API token](#internal-api-token) below. It is defence in depth, not a reason
  to expose the ports.
- Set a long random `AUTH_SECRET` (for example `openssl rand -base64 32`) and replace every
  default password in `.env` before exposing the stack.

## Internal API token

The API (port 3001) and the ingestion service (port 3002) have no users of their own.
They rely on the web app having checked permissions and, for the API, on the user
identity it forwards in `x-auto-ontology-user-id`. Without further protection, anything
that can reach those ports can call every route, including resets, deletes and chat SQL
against connected databases, and can claim to be any user.

`INTERNAL_API_TOKEN` closes that. When it is set, both services:

- answer **401** to every request that lacks a matching `x-internal-token` header, with a
  generic body and a WARNING in the log (method and path only, never the token);
- compare it in constant time (`hmac.compare_digest`);
- only trust `x-auto-ontology-user-id` on a request that carried the token.

Only the container and Kubernetes probes stay open, because the kubelet and Docker
cannot present a secret: `/api/health` and `/api/health/live` on the API,
`/health` and `/health/live` on the ingestion service. `/api/health` and `/health`
report the database error text when Postgres is down, so keep these ports off the
internet regardless. The web app attaches the token to every server-side call it makes
to the API (the browser never sees it), and the API attaches it to its own calls to the
ingestion service.

**Set it.** Generate one value and give it to the web app, the API and the ingestion
service:

```bash
openssl rand -hex 32
```

- Docker Compose: add `INTERNAL_API_TOKEN=<value>` to `.env`, then
  `docker compose up -d`. Compose passes it to all three services.
- Helm: set `internalApiToken`, or leave it empty and the chart generates one on first
  install and keeps it across upgrades (as it does for `authSecret`). With `helm template`
  or a GitOps tool that does not talk to the cluster, set it explicitly: otherwise each
  render produces a new value.
- Other callers of port 3001, such as `python -m dev_tools.evals run`, pass it as
  `--token` or the `INTERNAL_API_TOKEN` environment variable.

The services refuse to start when it is set but shorter than 32 characters, has fewer
than 10 distinct characters, or contains whitespace. Do not reuse `AUTH_SECRET`.

**Not set.** Behaviour is unchanged so existing deployments keep working, and the API
and the ingestion service log a WARNING at every startup saying they are unauthenticated.
New installs should set it.

**Upgrading.** Add the value to `.env` (or the Helm values) and restart all three
services together. A service that has the token refuses callers that do not, so during a
rolling restart the web app can briefly get 401s from the API until both sides carry the
same value.

**Rotating.** Generate a new value, set it everywhere, restart all three services. There
is no overlap window: requests fail until both sides have restarted.

**Out of scope.** The token authenticates the web app to the services; it is not a
per-user credential and is not scoped (anyone who holds it can call every route and
claim any identity). Traffic between the services is not encrypted, so a network
attacker on the private network can read the token; use a private network or a service
mesh for TLS. The token is not a substitute for keeping ports 3001 and 3002 private. The
MCP server is unaffected as long as it points at the web app (`AUTO_ONTOLOGY_API_URL`,
port 3000 by default): its calls carry the token through the proxy. Pointing it, or
anything else, straight at port 3001 requires presenting the token yourself.
