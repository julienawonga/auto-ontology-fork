<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Contributing to Auto Ontology

Thanks for your interest! Contributions of every size are welcome: bug reports, docs,
new connectors, UI improvements, tests.

- **Found a bug or have an idea?** Open an
  [issue](https://github.com/julienawonga/auto-ontology-fork/issues/new/choose) with the
  matching template.
- **Want to change code?** Open a pull request; for anything larger than a small fix,
  start with an issue so we can agree on the approach first.
- **Security issue?** Do not open a public issue; follow [SECURITY.md](SECURITY.md).

## Development setup

The fastest way to a working stack is Docker Compose; see the
[quick start](README.md#-quick-start). For local development:

| Part | Tooling | Run it |
|---|---|---|
| Backend (`auto_ontology/`) | Python 3.12, [uv](https://docs.astral.sh/uv/) | `uv sync`, then `uv run uvicorn auto_ontology.server.__main__:create_app --factory --reload --port 3001` |
| Ingestion service | same | `uv run python -m auto_ontology.ingestion_service` |
| Frontend (`frontend/`) | Node.js 22, [pnpm](https://pnpm.io/) | `pnpm install`, then `pnpm dev` |
| MCP server (`mcp/`) | uv | `cd mcp && uv sync && uv run auto-ontology-mcp` |

See the `Makefile` (`make help`) for build and migration targets.

## Before you open a pull request

Run the same checks as CI:

| Check | Command |
|---|---|
| Backend lint and format | `uv run ruff check auto_ontology/ && uv run ruff format --check auto_ontology/` |
| Backend tests | `uv run pytest auto_ontology dev_tools -q` |
| OpenAPI spec | `uv run python -m dev_tools.generate_backend_openapi` (commit the result if it changed) |
| Frontend | `cd frontend && npx prettier --check . && pnpm lint && pnpm build` |

If you change a route or its docstring, regenerate the committed OpenAPI specs: the
backend command above, and `pnpm openapi` in `frontend/` for the public API.

## Pull request guidelines

1. Branch from `main` and keep each pull request focused on one change.
2. Add or update tests for the behaviour you change.
3. Write commit messages in the imperative mood ("Add a Trino connector").
4. Describe what changed and how you tested it; include screenshots for UI changes.
5. New files carry an SPDX header with your name and the license:

   ```text
   SPDX-FileCopyrightText: Copyright (c) 2026, Your Name.
   SPDX-License-Identifier: Apache-2.0
   ```

   Files that came from the original project keep their existing copyright line.

All checks must pass before a pull request is merged.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
