<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AGENTS.md — Auto-Ontology agent entry point

Skills that teach an agent how to **set up** the current Auto-Ontology implementation and
**use** its ontology layer. They live in **`skills/`** (repo root), one directory
per skill, under the `ontology-*` namespace.

This file is for developers bringing Auto-Ontology up and partners embedding it in an
agent harness. Auto-Ontology **maintainers** (build, test, schema, frontend conventions)
stay on [`CLAUDE.md`](./CLAUDE.md). Do not fold those conventions into these
skills.

## Which skill to read

- [`skills/ontology-setup/`](./skills/ontology-setup/) — Helm (the official
  install), local Compose, developer `--dev` / `--ds`, MCP against an
  already-running instance, and troubleshooting. Start here when Auto-Ontology is not up yet or something failed
  during bring-up.
- [`skills/ontology-management/`](./skills/ontology-management/) — inspect, model,
  **modify**, and safely publish through glossary terms, SQL attributes,
  semantic relationships, model YAML import/export, and governed result
  definitions. MCP cannot write; this skill covers the approved write paths.
- [`skills/ontology-query/`](./skills/ontology-query/) — how an
  agent calls Auto-Ontology (MCP first, REST fallback), discovers meaning through the
  semantic layer, and validates SQL, rows, and answers. Start here when Auto-Ontology is
  already deployed.

## Workflow ownership and handoffs

| User action | Owning skill |
| --- | --- |
| Install, configure, connect, ingest, compile, prove readiness | `ontology-setup` |
| Inspect meaning and semantic relationships | `ontology-management` |
| Design concepts, mappings, relationships, measures, units, grain, and policies | `ontology-management` → `references/modeling.md` |
| Apply, verify, certify honestly, publish, and roll back | `ontology-management` → `references/publication.md` |
| Ask descriptive or diagnostic questions and validate SQL, rows, and prose | `ontology-query` → `references/query-validation.md` |

The composed lifecycle is install/connect/readiness → inspect/model → staged
apply/readback → query validation → governed publication → fresh query readback.
A step that lacks product support must return an explicit gap or receipt rather
than inventing an operation. Authentication, permissions, provenance, and error
handling remain shared concerns rather than separate overlapping skills.

## Sources of truth (do not duplicate)

Skills encode **workflow, order, and pitfalls**. They must not become a second
API catalog or a second installer.

| Job | Owner | Skills do |
| --- | --- | --- |
| Install commands | `DEPLOYMENT.md` (Helm, official), `docker-compose.yml`; `dev_tools/setup_env.sh` for developer workflows | Invoke them; do not write a new installer |
| Live read tools | `mcp/auto_ontology_mcp/tools.py` (handshake tool list) | Prefer MCP; sequence reads; never copy the tool table |
| REST shapes and permissions | `docs/openapi/auto-ontology-api.json` | Name the operation and permission; link; do not paste schemas |

MCP is read-only by design. Writes go through the Next.js `/api/...` surface.
The live MCP tool list at handshake is the source of truth if it disagrees
with anything in a skill.

## Skill files

Each skill directory holds:

- `SKILL.md` — the skill itself, with its `name:` frontmatter matching the
  directory name
- `skill-card.md` — a short description of what the skill does and its limits
- `evals/evals.json` — prompts and expected behavior used to check the skill
- `references/` and `assets/` — longer material that `SKILL.md` links to
- `BENCHMARK.md` — an evaluation report inherited from the upstream project. It
  is not regenerated here, so treat its numbers as historical.

Change the skill name in all of these together if you ever rename a skill.

Skills must stay free of staging hostnames, cluster names, Vault paths, and
credentials — they are public even when an Auto-Ontology deployment is
access-restricted.
