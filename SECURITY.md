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
([direct link](https://github.com/julienawonga/auto-ontology-fork/security/advisories/new)).
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
  authentication of its own; clients go through the web app on port 3000.
- Set a long random `AUTH_SECRET` (for example `openssl rand -base64 32`) and replace every
  default password in `.env` before exposing the stack.
