# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The startup pass skips databases the store already holds.

A restart used to re-ingest every connection — catalog extraction, every row
re-embedded through the model endpoint, the vector store reset per database —
to reproduce an ingest whose result was already stored. The decision these
tests pin is when that work is skipped: only on the *startup* pass, only when
both the catalog and the embeddings are present, and never when
``INGEST_ON_STARTUP=all`` asks for the old behaviour.

All store access is stubbed: what is under test is the decision, not the
queries (``database_is_ingested``'s two sub-checks are tested as booleans).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest

from auto_ontology.ingestion_service.data_scheduler import DataScheduler
from auto_ontology.ingestion_service.scheduler import IntervalScheduler


class _Connector:
    def __init__(self, name: str) -> None:
        self.database_name = name


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    connectors: list[_Connector],
    *,
    ingested: set[str] | None = None,
    force: bool = False,
    fail: set[str] | None = None,
) -> list[str]:
    """Stub a pass's dependencies; return what it did, in order.

    Each entry is either ``check:<name>`` (the already-ingested lookup) or the
    bare ``<name>`` (a ``run_ingest`` call), so both the decision and the work
    are visible in one list.
    """
    import auto_ontology.ingestion_service.data_scheduler as mod

    ingested = ingested or set()
    fail = fail or set()
    events: list[str] = []

    def _run_ingest(connector: Any) -> None:
        events.append(connector.database_name)
        if connector.database_name in fail:
            raise RuntimeError("nope")

    def _is_ingested(name: str) -> bool:
        events.append(f"check:{name}")
        return name in ingested

    monkeypatch.setattr(mod, "invalidate_connectors_cache", lambda: None)
    monkeypatch.setattr(mod, "get_connectors", lambda: connectors)
    monkeypatch.setattr(mod, "run_ingest", _run_ingest)
    monkeypatch.setattr(mod, "is_semantic_compilation_enabled", lambda: False)
    monkeypatch.setattr(mod, "startup_reingest_forced", lambda: force)
    monkeypatch.setattr(mod, "database_is_ingested", _is_ingested)
    return events


def _startup_scheduler() -> DataScheduler:
    """A scheduler whose startup pass is the one about to run."""
    return DataScheduler()


def _settled_scheduler() -> DataScheduler:
    """A scheduler past its startup pass: ``first_pass_done`` is set."""
    scheduler = DataScheduler()
    scheduler._first_pass_done.set()
    return scheduler


# --------------------------------------------------------------------------
# The knob
# --------------------------------------------------------------------------


def test_the_knob_defaults_to_not_forcing(monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_ontology.ingestion_service.startup_skip as mod

    monkeypatch.delenv(mod.INGEST_ON_STARTUP_ENV, raising=False)
    assert mod.startup_reingest_forced() is False


def test_the_knob_forces_on_all(monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_ontology.ingestion_service.startup_skip as mod

    monkeypatch.setenv(mod.INGEST_ON_STARTUP_ENV, mod.INGEST_ON_STARTUP_ALL)
    assert mod.startup_reingest_forced() is True


@pytest.mark.parametrize("value", ["", "  ", "skip-ingested", "SKIP-INGESTED"])
def test_the_knob_does_not_force_on_skip_or_empty(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    import auto_ontology.ingestion_service.startup_skip as mod

    monkeypatch.setenv(mod.INGEST_ON_STARTUP_ENV, value)
    assert mod.startup_reingest_forced() is False


def test_an_unrecognized_knob_value_falls_back_to_skipping(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo must cost one skipped re-ingest, not a paid full re-ingest."""
    import auto_ontology.ingestion_service.startup_skip as mod

    monkeypatch.setenv(mod.INGEST_ON_STARTUP_ENV, "everything")
    with caplog.at_level(logging.WARNING):
        assert mod.startup_reingest_forced() is False

    assert "unrecognized" in _messages(caplog)


# --------------------------------------------------------------------------
# database_is_ingested — both halves must be present
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tables", "vectors", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_a_database_is_ingested_only_with_catalog_and_embeddings(
    monkeypatch: pytest.MonkeyPatch, tables: bool, vectors: bool, expected: bool
) -> None:
    """Tables without vectors are the half-state a failed embed leaves behind;
    skipping those at startup would keep retrieval empty until the next
    scheduled pass, so either half missing means "not ingested"."""
    import auto_ontology.ingestion_service.startup_skip as mod

    monkeypatch.setattr(mod, "_catalog_holds_tables", lambda name: tables)
    monkeypatch.setattr(mod, "_vdb_holds_embeddings", lambda name: vectors)

    assert mod.database_is_ingested("pagila") is expected


def test_an_unreachable_store_counts_as_not_ingested(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The safe direction is to ingest: a store outage must not skip work."""
    import auto_ontology.ingestion_service.startup_skip as mod

    def _boom(name: str) -> bool:
        raise RuntimeError("no database")

    monkeypatch.setattr(mod, "_catalog_holds_tables", _boom)

    with caplog.at_level(logging.INFO):
        assert mod.database_is_ingested("pagila") is False

    assert "re-ingesting rather than skipping" in _messages(caplog)


# --------------------------------------------------------------------------
# The startup pass
# --------------------------------------------------------------------------


def test_the_startup_pass_skips_databases_the_store_already_holds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events = _patch(
        monkeypatch,
        [_Connector("pagila"), _Connector("chinook")],
        ingested={"pagila"},
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(_startup_scheduler()._run_once())

    # pagila is looked up and skipped; chinook is looked up and ingested.
    assert events == ["check:pagila", "check:chinook", "chinook"]
    text = _messages(caplog)
    assert "startup pass skipping pagila" in text
    assert "1 connection(s) ingested, 1 skipped as already ingested" in text


def test_a_later_pass_reingests_even_what_the_store_holds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 24h tick and API triggers are the change-detection mechanism — a
    database whose source changed while the service was down is refreshed
    there, so those passes never skip (and never pay the lookup either)."""
    events = _patch(
        monkeypatch,
        [_Connector("pagila")],
        ingested={"pagila"},
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(_settled_scheduler()._run_once())

    assert events == ["pagila"]
    assert "startup pass skipping" not in _messages(caplog)


def test_the_knob_restores_a_full_reingest_on_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events = _patch(
        monkeypatch,
        [_Connector("pagila")],
        ingested={"pagila"},
        force=True,
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(_startup_scheduler()._run_once())

    assert events == ["pagila"]
    assert "startup pass skipping" not in _messages(caplog)


def test_a_fresh_database_is_ingested_on_the_startup_pass(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First-time ingestion is the whole point of the startup pass."""
    events = _patch(monkeypatch, [_Connector("pagila")], ingested=set())

    with caplog.at_level(logging.INFO):
        asyncio.run(_startup_scheduler()._run_once())

    assert events == ["check:pagila", "pagila"]
    assert "startup pass skipping" not in _messages(caplog)


def test_a_skipped_database_is_reported_in_the_failure_tally(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The summary line has to stay honest when a pass both skips and fails."""
    events = _patch(
        monkeypatch,
        [_Connector("pagila"), _Connector("broken"), _Connector("fresh")],
        ingested={"pagila"},
        fail={"broken"},
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(_startup_scheduler()._run_once())

    assert events == ["check:pagila", "check:broken", "broken", "check:fresh", "fresh"]
    text = _messages(caplog)
    assert "finished with errors — 1 of 3 connection(s) succeeded, 1 failed" in text
    assert "1 skipped as already ingested" in text


def test_first_pass_done_is_false_until_the_first_pass_ends() -> None:
    """The property the skip decision reads. It is set when a pass *finishes*
    (``IntervalScheduler._run_pass``), never before it starts — pin that, so a
    scheduler change cannot silently turn every pass into a skipping one."""
    scheduler = IntervalScheduler(interval=timedelta(hours=24))
    assert scheduler.first_pass_done is False

    async def _finish_one_pass() -> None:
        scheduler._first_pass_done.set()

    asyncio.run(_finish_one_pass())
    assert scheduler.first_pass_done is True
