# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# ---------------------------------------------------------------------------
# Lexis image build / publish helpers
# ---------------------------------------------------------------------------
# These targets wrap the `docker buildx build` invocations needed to build
# the backend, frontend and MCP images and (optionally) push them to a
# container registry. Tagged releases are published to GHCR by
# .github/workflows/release.yml; these targets are for local builds and
# manual pushes. See DEPLOYMENT.md for the full deploy story.
#
# Common flow:
#
#   make build                                   # local builds (host arch)
#   echo $GITHUB_TOKEN | docker login ghcr.io -u <user> --password-stdin
#   make publish REGISTRY=ghcr.io/<owner>        # multi-arch push
#
# Variables (override on the command line):
#
#   REGISTRY        Registry path prefix, e.g. ghcr.io/<owner>
#                   (required for `publish*` targets)
#   TAG             Image tag. Defaults to `git describe`.
#   PYTHON_API_URL  Backend URL baked into the frontend's Next.js rewrites
#                   at build time. Defaults to http://auto-ontology:3001
#                   (matches the in-cluster service the helm chart creates).
#   PLATFORMS       Comma-separated platforms for multi-arch publish.
#                   Defaults to linux/amd64,linux/arm64.
# ---------------------------------------------------------------------------

REGISTRY       ?=
TAG            ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
PYTHON_API_URL ?= http://auto-ontology:3001
PLATFORMS      ?= linux/amd64,linux/arm64

BACKEND_IMAGE  := auto-ontology
FRONTEND_IMAGE := auto-ontology-frontend
MCP_IMAGE      := auto-ontology-mcp

# --tag prefix. Plain image name when REGISTRY is empty (local builds);
# fully-qualified when REGISTRY is set (publish builds).
ifeq ($(REGISTRY),)
  BACKEND_REF  := $(BACKEND_IMAGE):$(TAG)
  FRONTEND_REF := $(FRONTEND_IMAGE):$(TAG)
  MCP_REF      := $(MCP_IMAGE):$(TAG)
else
  BACKEND_REF  := $(REGISTRY)/$(BACKEND_IMAGE):$(TAG)
  FRONTEND_REF := $(REGISTRY)/$(FRONTEND_IMAGE):$(TAG)
  MCP_REF      := $(REGISTRY)/$(MCP_IMAGE):$(TAG)
endif

.PHONY: help builder build build-backend build-frontend build-mcp \
        publish publish-backend publish-frontend publish-mcp

help:
	@echo "Lexis image targets:"
	@echo "  make build               Build all images locally (host arch only)"
	@echo "  make build-backend       Build backend image locally"
	@echo "  make build-frontend      Build frontend image locally"
	@echo "  make build-mcp           Build MCP server image locally"
	@echo "  make publish             Build + push all images (multi-arch)"
	@echo "  make publish-backend     Build + push backend image"
	@echo "  make publish-frontend    Build + push frontend image"
	@echo "  make publish-mcp         Build + push MCP server image"
	@echo ""
	@echo "Schema targets (backend owns the \`auto_ontology\` schema; Prisma owns \`public\`):"
	@echo "  make migrate             Apply Alembic migrations (alembic upgrade head)"
	@echo "  make migrate-check       Fail if the schema has drifted from the metadata"
	@echo "  make migrate-revision m=\"...\"  Autogenerate a revision from auto_ontology/dal/schema.py"
	@echo
	@echo "Required for publish*: REGISTRY=ghcr.io/<owner> (after docker login ghcr.io)"
	@echo "Current REGISTRY=$(REGISTRY) TAG=$(TAG)"

# Ensure a docker-container builder exists for multi-arch builds.
builder:
	@docker buildx inspect auto-ontology-builder >/dev/null 2>&1 \
	  || docker buildx create --name auto-ontology-builder --driver docker-container --use
	@docker buildx use auto-ontology-builder

build-backend:
	docker buildx build -t $(BACKEND_REF) --load .

build-frontend:
	docker buildx build --build-arg PYTHON_API_URL=$(PYTHON_API_URL) \
	    -t $(FRONTEND_REF) --load ./frontend

build-mcp:
	docker buildx build -t $(MCP_REF) --load ./mcp

build: build-backend build-frontend build-mcp

publish-backend: require-registry builder
	docker buildx build --platform $(PLATFORMS) \
	    -t $(BACKEND_REF) --push .

publish-frontend: require-registry builder
	docker buildx build --platform $(PLATFORMS) \
	    --build-arg PYTHON_API_URL=$(PYTHON_API_URL) \
	    -t $(FRONTEND_REF) --push ./frontend

publish-mcp: require-registry builder
	docker buildx build --platform $(PLATFORMS) \
	    -t $(MCP_REF) --push ./mcp

publish: publish-backend publish-frontend publish-mcp

# Internal: fail fast if REGISTRY is missing on a publish target.
.PHONY: require-registry
require-registry:
	@if [ -z "$(REGISTRY)" ]; then \
	  echo "ERROR: REGISTRY is required for publish targets."; \
	  echo "       e.g. make publish REGISTRY=ghcr.io/<owner>"; \
	  exit 2; \
	fi

# --- Schema migrations -------------------------------------------------
# The backend owns the `auto_ontology` Postgres schema. Prisma owns `public` and migrates
# separately (frontend/prisma/migrate.sh); the two are independent.

migrate:
	uv run alembic upgrade head

# Fails when auto_ontology/dal/schema.py and the database disagree. Run this in CI:
# a schema change committed without its migration is invisible until deploy.
migrate-check:
	uv run alembic check

migrate-revision:
	@test -n "$(m)" || (echo 'usage: make migrate-revision m="what changed"' >&2; exit 2)
	uv run alembic revision --autogenerate -m "$(m)"

.PHONY: migrate migrate-check migrate-revision


