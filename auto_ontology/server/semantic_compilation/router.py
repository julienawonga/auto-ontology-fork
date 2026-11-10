# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""API route for triggering semantic compilation on the ingestion service.

The settings UI stores the on/off flag directly in Postgres (via Prisma); these
routes only relay the "run it now" and "reset the layer" requests to the
ingestion service, mirroring how connection changes trigger ingest (see
``connections/service.py``).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from auto_ontology.dal.datasources import fetch_databases
from auto_ontology.dal.terms import semantic_layer_calculated
from auto_ontology.ingestion_service.history import (
    get_last_failure_if_most_recent,
    get_last_successful_run,
)
from auto_ontology.server.ingestion.proxy import (
    is_semantic_compilation_running,
    trigger_semantic_compile,
    trigger_semantic_reset,
)
from auto_ontology.semantic.embed import (
    build_semantic_embedder,
    embed_all_semantic_nodes,
)
from auto_ontology.semantic.index_health import check_semantic_indexes
from auto_ontology.server.responses import (
    SemanticReindexResponse,
    SemanticStatusResponse,
    StatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/semantic-compilation/trigger", status_code=202, response_model=StatusResponse
)
async def trigger() -> dict[str, str]:
    """Ask the ingestion service to run (and schedule) semantic compilation."""
    trigger_semantic_compile()
    return {"status": "accepted"}


@router.post(
    "/semantic-compilation/reset", status_code=202, response_model=StatusResponse
)
async def reset() -> dict[str, str]:
    """Ask the ingestion service to rebuild every database's semantic layer.

    The service deletes the semantic nodes and embeddings and compiles them
    again, so the layer is rebuilt without waiting for the next scheduled run.
    """
    trigger_semantic_reset()
    return {"status": "accepted"}


@router.post("/semantic-compilation/reindex", response_model=SemanticReindexResponse)
def reindex(
    database_name: str = Query(
        description="Name of the catalog database whose semantic index to rebuild."
    ),
) -> dict[str, Any]:
    """Re-embed one database's Terms and ColumnAttributes into the semantic index.

    Compilation embeds a table's Terms once, when the table is first compiled,
    so embeddings lost to a failing endpoint are never retried; this rebuilds
    them from the Terms already stored, without recompiling anything.

    Additive and repeatable: each row replaces the one stored under the same
    id, and nothing else in the index is touched. Runs synchronously so the
    result can say how many rows were ``written`` out of those ``expected`` —
    fewer means the embedding endpoint did not return a vector for every row,
    and the rows it did return are still written.

    404 for a database the catalog does not hold, 409 while a compilation pass
    is running (both would be writing the same rows), 503 when no embedding
    endpoint is configured.
    """
    if database_name not in {db["name"] for db in fetch_databases()}:
        raise HTTPException(
            status_code=404, detail=f"Unknown database {database_name!r}"
        )
    if is_semantic_compilation_running():
        raise HTTPException(
            status_code=409,
            detail="Semantic compilation is running; retry when it has finished",
        )
    embedder = build_semantic_embedder(database_name, reset=False)
    if embedder is None:
        raise HTTPException(
            status_code=503, detail="Embedding is not configured (no API key)"
        )
    result = embed_all_semantic_nodes(embedder)
    return {"database_name": database_name, **asdict(result)}


def _index_status() -> list[dict[str, Any]] | None:
    """Per-database index drift, or ``None`` when it cannot be read.

    Never raises: the chat page gates on this endpoint, and a vector store
    that cannot be queried must not take ``calculated`` down with it.
    """
    try:
        return [
            {**asdict(health), "missing": health.missing}
            for health in check_semantic_indexes()
        ]
    except Exception:
        logger.exception("Failed to compare the semantic index with the ontology")
        return None


@router.get("/semantic-compilation/status", response_model=SemanticStatusResponse)
def semantic_status() -> dict[str, Any]:
    """Report whether the semantic layer has been calculated (any Term exists),
    whether the ingestion service is compiling it right now, when the last
    pass completed successfully, when it last failed, and how much of each
    database's ontology is present in the semantic vector index.

    ``running`` needs the live ingestion process, so it goes through the HTTP
    proxy and degrades to ``False`` if that's unreachable. ``last_success_at``
    and ``last_failure_at`` are read straight from the ``semantic_compilation_history``
    table instead — a temporarily unreachable ingestion service has no bearing
    on Postgres, so history stays visible even while it's down (see
    ``auto_ontology/server/ingestion/proxy.py``).

    ``index`` lists, per database, the Terms and ColumnAttributes ``expected``
    from the relational store, how many are ``indexed`` and how many are
    ``missing``. A non-zero ``missing`` means chat cannot retrieve part of the
    semantic layer; ``POST /semantic-compilation/reindex`` rebuilds it. ``null``
    when the comparison could not be made.
    """
    return {
        "calculated": semantic_layer_calculated(),
        "running": is_semantic_compilation_running(),
        "last_success_at": get_last_successful_run(),
        "last_failure_at": get_last_failure_if_most_recent(),
        "index": _index_status(),
    }
