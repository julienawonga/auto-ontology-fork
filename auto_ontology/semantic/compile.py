# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unified entry point for semantic layer compilation."""

from __future__ import annotations

import logging
import os
import time

from auto_ontology.infra.feature_flags import (
    SQL_ATTRIBUTE_SUGGESTION_ENABLED_KEY,
    is_sql_attribute_suggestion_enabled,
)
from auto_ontology.semantic.bridge_tables import build_bridge_tables_sql_attributes
from auto_ontology.semantic.cancellation import is_cancelled
from auto_ontology.semantic.domain import DomainSummary, load_domain_summary
from auto_ontology.semantic.embed import build_semantic_embedder
from auto_ontology.semantic.index_health import check_semantic_index
from auto_ontology.semantic.semantic_fk import resolve_semantic_fks

from auto_ontology.semantic.pipeline import compile_semantic_layer
from auto_ontology.utils.llm_invoke import limit_inflight

logger = logging.getLogger(__name__)

# Semantic compilation runs tables in parallel and fans out again within each,
# so its request rate is a product rather than a sum and can outrun what the
# endpoint will serve -- previously seen as HTTP 503 ResourceExhausted. The
# bound lives here, applied for the duration of a compilation, rather than at
# the shared invoke call site where it also throttled callers that already
# limit their own concurrency.
SEMANTIC_LLM_MAX_INFLIGHT = int(os.environ.get("SEMANTIC_LLM_MAX_INFLIGHT", "6"))


class SemanticIndexIncompleteError(RuntimeError):
    """The compilation ran, but Terms or ColumnAttributes are not in the index.

    Raised so the pass is recorded as failed. Embedding is best-effort per
    table -- a rejected request is logged and the table carries on -- which is
    right for one table and wrong as a verdict: with an embed endpoint refusing
    every request, 21 consecutive passes were recorded as succeeded over an
    index that held nothing.
    """


def run_semantic_compilation(
    database_name: str,
    *,
    domain_summary: DomainSummary | None = None,
) -> int:
    """Compile semantic taxonomy, embed all nodes into the VDB, then resolve FK edges.

    Returns the number of tables processed. Raises
    :class:`SemanticIndexIncompleteError` when the stages completed but the
    database's Terms and ColumnAttributes are not all in the semantic index.
    """
    with limit_inflight(SEMANTIC_LLM_MAX_INFLIGHT):
        return _run_semantic_compilation(database_name, domain_summary=domain_summary)


def _run_semantic_compilation(
    database_name: str,
    *,
    domain_summary: DomainSummary | None = None,
) -> int:
    """Body of :func:`run_semantic_compilation`, run under the concurrency bound."""
    summary = domain_summary or load_domain_summary(database_name)

    started = time.monotonic()
    embedder = build_semantic_embedder(database_name, reset=False)

    logger.info("=" * 60)
    logger.info(
        "Semantic compilation (database=%r)",
        database_name,
    )
    logger.info("=" * 60)

    count = compile_semantic_layer(
        database_name,
        domain_summary=summary,
        embedder=embedder,
    )

    logger.info("=" * 60)
    logger.info("Semantic compilation finished — %d table visits", count)
    logger.info("=" * 60)

    # The three stages below are the long tail — FK resolution alone has run
    # past 300s on one database — and none of them is required for the Terms
    # just written to be usable. A cancelled pass skips straight to the summary
    # rather than spending minutes on work nobody is waiting for.
    if is_cancelled():
        logger.info(
            "Semantic compilation cancelled (database=%r) — skipping the FK, "
            "SqlAttribute and bridge-table stages; %d table(s) compiled in %.1fs",
            database_name,
            count,
            time.monotonic() - started,
        )
        return count

    logger.info("=" * 60)
    logger.info("Resolving semantic FK edges…")
    logger.info("=" * 60)
    fk_count = resolve_semantic_fks(database_name)
    logger.info("Semantic FK edges created: %d", fk_count)

    attr_count = 0
    if is_sql_attribute_suggestion_enabled():
        from auto_ontology.semantic.sql_attribute_suggester import (
            suggest_sql_attributes,
        )

        logger.info("=" * 60)
        logger.info("Suggesting SqlAttributes from query history…")
        logger.info("=" * 60)
        attr_count = suggest_sql_attributes(database_name)
        logger.info("New SqlAttribute nodes written: %d", attr_count)
    else:
        logger.info(
            "SqlAttribute suggestion from query history is off "
            "(configuration %r); no attribute written",
            SQL_ATTRIBUTE_SUGGESTION_ENABLED_KEY,
        )

    logger.info("=" * 60)
    logger.info("Building bridge tables sql attributes")
    logger.info("=" * 60)
    bridge_table_count = build_bridge_tables_sql_attributes(database_name)
    logger.info("Bridge tables built: %d", bridge_table_count)

    # Checked against the whole database, not just this run's tables, and after
    # the stages above so that none of them is skipped on its account. A table
    # is only ever compiled -- and embedded -- once, so a gap left by an earlier
    # pass is not repaired by this one, and a pass that compiles nothing would
    # otherwise succeed on top of it. Skipped when embedding is switched off:
    # the index is then empty by configuration, which is already warned about.
    if embedder is not None:
        index = check_semantic_index(database_name)
        if index.missing:
            raise SemanticIndexIncompleteError(
                f"semantic index for database {database_name!r} is missing "
                f"{index.missing} of {index.expected} Term/ColumnAttribute row(s) "
                "— their embeddings were not written; check the embedding "
                "endpoint, then rebuild with POST /api/semantic-compilation/reindex"
            )

    # One line carrying every stage's result. Until now the last thing a
    # completed compilation logged was the bridge-table count, which is a stage
    # result and not a verdict — there was no way to tell a finished run from one
    # that died after the bridge-table stage. Every stage above propagates its
    # exceptions, so reaching this means all four stages completed.
    logger.info("=" * 60)
    logger.info(
        "Semantic calculation finished successfully (database=%r) — "
        "%d table(s), %d semantic FK edge(s), %d SqlAttribute(s), "
        "%d bridge table(s) in %.1fs",
        database_name,
        count,
        fk_count,
        attr_count,
        bridge_table_count,
        time.monotonic() - started,
    )
    logger.info("=" * 60)

    return count
