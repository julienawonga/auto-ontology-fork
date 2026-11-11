# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Whether the ingest scheduler's *startup* pass should re-ingest a database
the store already holds.

The service used to re-ingest every configured connection the moment it booted:
catalog extraction for each, then every row re-embedded through the model
endpoint and the data-layer vector store reset per database. On a deployment of
twenty databases that is thousands of paid embedding requests to reproduce an
ingest whose result is already in the store — and it is paid again on every
container restart, of which a ``restart: unless-stopped`` policy produces
plenty.

The startup pass now skips a database whose catalog *and* data-layer embeddings
are already present, because nothing the pass would write differs from what is
stored. First-time ingestion is unaffected — a database missing from the
catalog has nothing to skip. Deliberately not a source diff: deciding "did the
source change since the last ingest" would require extracting the source, which
is the expensive half of the work this exists to avoid. Changes are picked up
by the mechanisms that already exist for that job: the 24h scheduled pass
re-ingests everything, and ``POST /ingest`` re-ingests one connection on
demand. ``INGEST_ON_STARTUP=all`` restores the old full re-ingest on boot.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import func, select

from auto_ontology.catalog.constants import Labels
from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store
from auto_ontology.vdb import get_data_vdb

logger = logging.getLogger(__name__)

#: Environment variable selecting the startup pass's behaviour.
INGEST_ON_STARTUP_ENV = "INGEST_ON_STARTUP"

#: ``INGEST_ON_STARTUP=all`` — re-ingest every connection at startup, the
#: behaviour the skip replaced.
INGEST_ON_STARTUP_ALL = "all"

#: ``INGEST_ON_STARTUP=skip-ingested`` (the default) — ingest only the
#: databases the store does not already hold.
INGEST_ON_STARTUP_SKIP_INGESTED = "skip-ingested"


def startup_reingest_forced() -> bool:
    """Whether ``INGEST_ON_STARTUP=all`` asks the startup pass to re-ingest.

    An unset, empty or unrecognized value means "skip" — the knob guards an
    optimisation, so a typo in it should cost one skipped re-ingest (fixable
    through ``POST /ingest`` or the next scheduled pass), not a service that
    re-embeds a whole deployment over an operator's spelling mistake.
    """
    raw = os.environ.get(INGEST_ON_STARTUP_ENV, "")
    value = raw.strip().lower() or INGEST_ON_STARTUP_SKIP_INGESTED
    if value == INGEST_ON_STARTUP_ALL:
        return True
    if value != INGEST_ON_STARTUP_SKIP_INGESTED:
        logger.warning(
            "ingest: unrecognized %s=%r (expected %r or %r); using %r",
            INGEST_ON_STARTUP_ENV,
            raw,
            INGEST_ON_STARTUP_SKIP_INGESTED,
            INGEST_ON_STARTUP_ALL,
            INGEST_ON_STARTUP_SKIP_INGESTED,
        )
    return False


def _catalog_holds_tables(database_name: str) -> bool:
    """Whether the catalog store has at least one table for the database.

    ``catalog_database`` rows exist before any ingest — creating a UI-managed
    connection inserts one — so the database row alone says nothing; the
    *tables* are what an ingest writes.
    """
    statement = (
        select(func.count().label("tables"))
        .select_from(
            s.catalog_table.join(
                s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id
            ).join(
                s.catalog_database,
                s.catalog_database.c.id == s.catalog_schema.c.database_id,
            )
        )
        .where(s.catalog_database.c.name == database_name)
    )
    rows = store().query_read(statement)
    return bool(rows) and int(rows[0]["tables"] or 0) > 0


def _vdb_holds_embeddings(database_name: str) -> bool:
    """Whether the data-layer vector store holds rows for the database."""
    ids = get_data_vdb().ids_by_database(database_name, [Labels.TABLE, Labels.COLUMN])
    return bool(ids)


def database_is_ingested(database_name: str) -> bool:
    """True when the catalog holds the database's tables *and* their embeddings.

    Both halves, because either can be missing on its own: a catalog written by
    a run whose embed step then failed leaves a database with tables but no
    vectors, and skipping it at startup would keep retrieval empty until the
    next scheduled pass. A store that cannot be reached counts as "not
    ingested" for the same reason — the safe direction is to ingest.
    """
    try:
        return _catalog_holds_tables(database_name) and _vdb_holds_embeddings(
            database_name
        )
    except Exception:
        logger.exception(
            "ingest: could not tell whether database %s is already ingested; "
            "re-ingesting rather than skipping",
            database_name,
        )
        return False
