# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the source-connection limits (no database needed).

The same behaviour against a real Postgres is in
``test_source_connection_pooling.py``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from pytest import MonkeyPatch

from auto_ontology.connectors import registry, source_limits
from auto_ontology.connectors.postgres import PostgresDatabase
from auto_ontology.connectors.source_limits import (
    SourceConnectionTimeout,
    SourceLimiter,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: MonkeyPatch) -> None:
    for name in (
        "SOURCE_POOL_MAX_SIZE",
        "SOURCE_POOL_MAX_IDLE_S",
        "SOURCE_MAX_CONNECTIONS",
        "SOURCE_CONNECTION_WAIT_S",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    assert source_limits.pool_max_size() == 2
    assert source_limits.pool_max_idle_s() == 30.0
    assert source_limits.max_connections() == 12
    assert source_limits.wait_s() == 60.0


def test_env_overrides(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_POOL_MAX_SIZE", "5")
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("SOURCE_CONNECTION_WAIT_S", "1.5")
    monkeypatch.setenv("SOURCE_POOL_MAX_IDLE_S", "9")

    assert source_limits.pool_max_size() == 5
    assert source_limits.max_connections() == 7
    assert source_limits.wait_s() == 1.5
    assert source_limits.pool_max_idle_s() == 9.0


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "nan", "inf", ""])
def test_a_bad_value_falls_back_to_the_default_and_never_disables_the_cap(
    monkeypatch: MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", bad)

    assert source_limits.max_connections() == source_limits.DEFAULT_MAX_CONNECTIONS


def _run_workers(
    limiters: list[SourceLimiter], n: int, hold_s: float
) -> tuple[int, list[BaseException]]:
    peak = 0
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work(limiter: SourceLimiter) -> None:
        nonlocal peak
        try:
            with limiter.slot("src"):
                with lock:
                    peak = max(peak, source_limits.connections_in_use())
                time.sleep(hold_s)
        except BaseException as exc:  # noqa: BLE001 - returned to the test
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(limiters[i % len(limiters)],))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return peak, errors


def test_the_cap_holds_and_surplus_requests_wait(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", "3")
    monkeypatch.setenv("SOURCE_CONNECTION_WAIT_S", "30")
    limiters = [SourceLimiter(2) for _ in range(5)]

    started = time.monotonic()
    peak, errors = _run_workers(limiters, n=30, hold_s=0.05)

    assert errors == []
    assert peak == 3
    assert time.monotonic() - started >= (30 / 3) * 0.05 * 0.9
    assert source_limits.connections_in_use() == 0


def test_per_source_size_bounds_one_source(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", "50")
    peak, errors = _run_workers([SourceLimiter(2)], n=10, hold_s=0.03)

    assert errors == []
    assert peak == 2


def test_timeout_is_a_clear_error_that_names_the_knobs(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", "1")
    limiter = SourceLimiter(1)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter.slot("pagila"):
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    entered.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(SourceConnectionTimeout) as caught:
            with SourceLimiter(1).slot("pagila", wait=0.2):
                pass
        assert 0.15 <= time.monotonic() - started < 2
    finally:
        release.set()
        holder.join()

    message = str(caught.value)
    assert "pagila" in message and "SOURCE_CONNECTION_WAIT_S" in message
    assert source_limits.connections_in_use() == 0  # the failed wait leaked no slot


def test_a_timed_out_per_source_wait_does_not_take_a_global_slot(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_MAX_CONNECTIONS", "5")
    limiter = SourceLimiter(1)
    with limiter.slot("a"):
        with pytest.raises(SourceConnectionTimeout):
            with limiter.slot("a", wait=0.1):
                pass
        assert source_limits.connections_in_use() == 1
    assert source_limits.connections_in_use() == 0


def test_a_failing_statement_releases_its_slot() -> None:
    limiter = SourceLimiter(1)

    with pytest.raises(ValueError):
        with limiter.slot("a"):
            raise ValueError("boom")

    assert source_limits.connections_in_use() == 0
    with limiter.slot("a", wait=0.1):  # per-source slot is free again too
        pass


def test_timeout_is_not_a_statement_timeout() -> None:
    from auto_ontology.connectors.base import StatementTimeout

    assert not issubclass(SourceConnectionTimeout, StatementTimeout)


# ----------------------------------------------------------------------
# Construction opens nothing
# ----------------------------------------------------------------------


def _forbid_connecting(monkeypatch: MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a connector must not connect while idle")

    monkeypatch.setattr("auto_ontology.connectors.postgres.ConnectionPool", refuse)
    monkeypatch.setattr("auto_ontology.connectors.postgres.psycopg.connect", refuse)


def test_constructing_and_naming_a_postgres_source_opens_nothing(
    monkeypatch: MonkeyPatch,
) -> None:
    _forbid_connecting(monkeypatch)

    database = PostgresDatabase("postgresql://u:p%40ss@db.invalid:5432/sales")

    assert database.database_name == "sales"
    database.close()  # closing a never-used connector is a no-op


def test_a_uri_without_a_database_asks_the_server_once_and_lazily(
    monkeypatch: MonkeyPatch,
) -> None:
    import pandas as pd

    database = PostgresDatabase("postgresql://u:p@db.invalid:5432")
    calls: list[str] = []

    def fake_execute(sql: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(sql)
        return pd.DataFrame([("resolved",)])

    monkeypatch.setattr(database, "execute", fake_execute)

    assert database.database_name == "resolved"
    assert database.database_name == "resolved"
    assert calls == ["SELECT current_database()"]


# ----------------------------------------------------------------------
# Connector construction errors surface as themselves
# ----------------------------------------------------------------------


@pytest.fixture
def no_cache(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_connectors", None)


def _store_down(monkeypatch: MonkeyPatch) -> None:
    def boom() -> list[dict]:
        raise ConnectionError("couldn't get a connection after 30.00 sec")

    monkeypatch.setattr("auto_ontology.dal.connections.list_connections", boom)


def test_an_unreadable_store_is_an_error_not_an_empty_connector_list(
    monkeypatch: MonkeyPatch, no_cache: None
) -> None:
    _store_down(monkeypatch)
    monkeypatch.delenv("CONNECTION_STRINGS", raising=False)

    with pytest.raises(registry.ConnectorLoadError, match="couldn't get a connection"):
        registry.get_connectors()
    # Not cached: once the store is back the next call must work.
    assert registry._connectors is None  # noqa: SLF001


def test_an_unreadable_store_still_falls_back_to_the_env_connections(
    monkeypatch: MonkeyPatch, no_cache: None
) -> None:
    _store_down(monkeypatch)
    monkeypatch.setenv("CONNECTION_STRINGS", "postgresql://u:p@db.invalid:5432/sales")

    connectors = registry.get_connectors()

    assert [c.database_name for c in connectors] == ["sales"]
    registry.invalidate_connectors_cache()


def test_nothing_configured_is_still_a_legitimate_empty_list(
    monkeypatch: MonkeyPatch, no_cache: None
) -> None:
    monkeypatch.setattr("auto_ontology.dal.connections.list_connections", lambda: [])
    monkeypatch.delenv("CONNECTION_STRINGS", raising=False)

    assert registry.get_connectors() == []


def test_a_connector_construction_error_surfaces_as_itself(
    monkeypatch: MonkeyPatch, no_cache: None
) -> None:
    monkeypatch.setattr(
        "auto_ontology.dal.connections.list_connections",
        lambda: [
            {
                "type": "postgres",
                "host": "h",
                "user": "u",
                "password": "p",
                "database": "d",
            }
        ],
    )

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("driver exploded")

    monkeypatch.setattr(registry, "create_connector", refuse)

    with pytest.raises(PermissionError, match="driver exploded"):
        registry.get_connectors()
    assert registry._connectors is None  # noqa: SLF001
