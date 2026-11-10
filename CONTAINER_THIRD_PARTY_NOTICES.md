<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Container Third-Party Notices

This file covers what the Auto-Ontology container images add on top of their
base image. The application's own open-source dependencies (Python and npm
packages) are listed separately in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Every image ships these files under `/licenses/`: `LICENSE`,
`THIRD_PARTY_NOTICES.md`, this file, and `OPEN_SOURCE_NOTICE`.

## Source code

Source code for the open-source components added on top of the base image is
archived by NVIDIA at https://opensource.nvidia.com/oss/teams/nvidia, as stated
in `OPEN_SOURCE_NOTICE`. Auto-Ontology's own source code is at
https://github.com/NVIDIA/auto-ontology.

## Base image

All three images are built on Docker Official Images (Debian 12 "bookworm"),
pinned by digest in each Dockerfile:

| Image | Base |
| --- | --- |
| `auto-ontology` (backend and ingestion) | `debian:bookworm-slim` |
| `auto-ontology-frontend` | `node:22-bookworm-slim` |
| `auto-ontology-mcp` | `debian:bookworm-slim` |

Source code for the base images' Debian packages is available from Debian
(https://sources.debian.org); the `node` image is built from
https://github.com/nodejs/docker-node.

## Components added on top of the base image

### `auto-ontology` (backend and ingestion) — `Dockerfile`

| Component | Version | License | Project URL |
| --- | --- | --- | --- |
| CPython (python-build-standalone, installed by uv) | 3.12 | PSF-2.0, plus the licenses of its bundled libraries | https://github.com/astral-sh/python-build-standalone |
| Python packages in `/opt/venv` | per `uv.lock` | see `THIRD_PARTY_NOTICES.md` (Backend) | — |
| `ca-certificates` (Debian package) | bookworm | MPL-2.0 (certificate data), GPL-2.0-or-later (scripts) | https://packages.debian.org/bookworm/ca-certificates |
| `libpq5` (Debian package) | bookworm | PostgreSQL License | https://www.postgresql.org |
| `tini` (Debian package) | bookworm | MIT | https://github.com/krallin/tini |

### `auto-ontology-frontend` — `frontend/Dockerfile`

| Component | Version | License | Project URL |
| --- | --- | --- | --- |
| Node.js, including its bundled npm and Corepack (from the `node` base image) | 22.x | MIT, plus the licenses of its bundled dependencies | https://github.com/nodejs/node |
| npm packages in the Next.js standalone bundle | per `frontend/pnpm-lock.yaml` | see `THIRD_PARTY_NOTICES.md` (Frontend runtime) | — |
| Prisma CLI and engines (`/app/migrate`) | 7.7.0 | Apache-2.0 | https://github.com/prisma/prisma |
| dotenv (`/app/migrate`) | 17.4.2 | BSD-2-Clause | https://github.com/motdotla/dotenv |
| Geist and Geist Mono fonts (bundled by `next/font`) | — | SIL Open Font License 1.1 | https://github.com/vercel/geist-font |
| Plus Jakarta Sans font (bundled by `next/font`, Chat page) | — | SIL Open Font License 1.1 | https://github.com/tokotype/PlusJakartaSans |
| JetBrains Mono font (bundled by `next/font`, Chat page) | — | SIL Open Font License 1.1 | https://github.com/JetBrains/JetBrainsMono |
| `ca-certificates` (Debian package) | bookworm | MPL-2.0 (certificate data), GPL-2.0-or-later (scripts) | https://packages.debian.org/bookworm/ca-certificates |
| `tini` (Debian package) | bookworm | MIT | https://github.com/krallin/tini |

### `auto-ontology-mcp` — `mcp/Dockerfile`

| Component | Version | License | Project URL |
| --- | --- | --- | --- |
| CPython (python-build-standalone, installed by uv) | 3.12 | PSF-2.0, plus the licenses of its bundled libraries | https://github.com/astral-sh/python-build-standalone |
| Python packages in `/opt/venv` | per `mcp/uv.lock` | see `THIRD_PARTY_NOTICES.md` (MCP server) | — |
| `ca-certificates` (Debian package) | bookworm | MPL-2.0 (certificate data), GPL-2.0-or-later (scripts) | https://packages.debian.org/bookworm/ca-certificates |
| `tini` (Debian package) | bookworm | MIT | https://github.com/krallin/tini |

Build-only tools (uv, compilers, `git`, pnpm) are used in earlier
build stages and are not present in the final images. The frontend image keeps
the npm and Corepack that ship with Node.js.

## Updating this file

Update this file whenever a Dockerfile changes its base image, its apt
packages, or the runtimes it installs. `THIRD_PARTY_NOTICES.md` tracks
dependency changes.
