# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chat SQL logging reports the statement and the credential behind it."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd
import pytest
from pytest import LogCaptureFixture

from auto_ontology.connectors.databricks import AUTH_SSO_FEDERATION, AUTH_STORED_TOKEN
from auto_ontology.infra.feature_flags import DEFAULT_SQL_QUERY_TIMEOUT_SECONDS
from auto_ontology.retrieval.text_to_sql import chat_sql
from auto_ontology.retrieval.text_to_sql.chat_sql import (
    _MAX_LOGGED_SQL_CHARS,
    TRUNCATED_AT_ATTR,
    execute_chat_sql,
    log_chat_sql,
    truncate_sql,
)
from auto_ontology.retrieval.text_to_sql.sql_guard import UnsafeSqlError

_LOGGER = "auto_ontology.retrieval.text_to_sql.chat_sql"


@pytest.fixture(autouse=True)
def row_limit(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub the row-limit read (it would hit the database); append to change it."""
    stored = [10_000]
    monkeypatch.setattr(chat_sql, "get_sql_result_max_rows", lambda: stored[-1])
    return stored


@pytest.fixture
def configured_timeout(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub the settings read; append to the list to change the stored value."""
    stored = [DEFAULT_SQL_QUERY_TIMEOUT_SECONDS]
    monkeypatch.setattr(chat_sql, "get_sql_query_timeout_seconds", lambda: stored[-1])
    return stored


def test_logs_sso_federated_credential(caplog: LogCaptureFixture) -> None:
    connector = SimpleNamespace(database_name="kdc_ca1", auth_mode=AUTH_SSO_FEDERATION)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_chat_sql(connector, "SELECT 1")

    assert f"[kdc_ca1] Executing chat SQL as {AUTH_SSO_FEDERATION}:\nSELECT 1" in [
        r.getMessage() for r in caplog.records
    ]


def test_logs_stored_token_credential(caplog: LogCaptureFixture) -> None:
    connector = SimpleNamespace(database_name="kdc_ca1", auth_mode=AUTH_STORED_TOKEN)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_chat_sql(connector, "SELECT 1")

    assert f"[kdc_ca1] Executing chat SQL as {AUTH_STORED_TOKEN}:\nSELECT 1" in [
        r.getMessage() for r in caplog.records
    ]


def test_omits_credential_for_connectors_without_auth_mode(
    caplog: LogCaptureFixture,
) -> None:
    """Postgres et al. have one fixed credential — don't imply otherwise."""
    connector = SimpleNamespace(database_name="mydb")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_chat_sql(connector, "SELECT 1")

    messages = [r.getMessage() for r in caplog.records]
    assert "[mydb] Executing chat SQL:\nSELECT 1" in messages
    assert not any("as the" in m for m in messages)


def test_probe_queries_are_labelled(caplog: LogCaptureFixture) -> None:
    connector = SimpleNamespace(database_name="kdc_ca1", auth_mode=AUTH_STORED_TOKEN)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_chat_sql(connector, "SELECT 1", kind="probe SQL")

    assert any("Executing probe SQL as" in r.getMessage() for r in caplog.records)


def test_unknown_database_name_does_not_crash(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_chat_sql(SimpleNamespace(), "SELECT 1")

    assert "[unknown] Executing chat SQL:\nSELECT 1" in [
        r.getMessage() for r in caplog.records
    ]


def test_long_sql_is_truncated() -> None:
    assert truncate_sql("SELECT 1") == "SELECT 1"

    truncated = truncate_sql("x" * (_MAX_LOGGED_SQL_CHARS + 50))
    assert truncated.startswith("x" * 100)
    assert "[50 more chars]" in truncated


class _Recorder:
    """Connector stub recording how ``execute`` was called."""

    database_name = "kdc_ca1"

    def __init__(self, supports_timeout: bool) -> None:
        self.supports_statement_timeout = supports_timeout
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, **kwargs: object) -> str:
        self.calls.append((sql, kwargs))
        return "df"


def test_chat_sql_is_capped_at_30_seconds(configured_timeout: list[int]) -> None:
    connector = _Recorder(supports_timeout=True)

    assert execute_chat_sql(connector, "SELECT 1") == "df"
    assert connector.calls == [
        ("SELECT 1", {"timeout_s": DEFAULT_SQL_QUERY_TIMEOUT_SECONDS})
    ]
    assert DEFAULT_SQL_QUERY_TIMEOUT_SECONDS == 30


def test_chat_sql_uses_the_configured_timeout(configured_timeout: list[int]) -> None:
    """Read per statement, so a change in Agent Settings applies to the next one."""
    connector = _Recorder(supports_timeout=True)

    configured_timeout.append(90)
    execute_chat_sql(connector, "SELECT 1")
    configured_timeout.append(5)
    execute_chat_sql(connector, "SELECT 2")

    assert [kwargs for _, kwargs in connector.calls] == [
        {"timeout_s": 90},
        {"timeout_s": 5},
    ]


def test_without_settings_the_default_is_30_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No frontend means no ``configurations`` table; the read must fall back."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(
        "auto_ontology.infra.feature_flags._read_configuration_row", _boom
    )
    connector = _Recorder(supports_timeout=True)

    execute_chat_sql(connector, "SELECT 1")

    assert connector.calls == [("SELECT 1", {"timeout_s": 30})]


def test_connectors_without_timeout_support_are_called_unchanged() -> None:
    """A connector without a cap must not get an argument it would reject."""
    connector = _Recorder(supports_timeout=False)

    execute_chat_sql(connector, "SELECT 1")

    assert connector.calls == [("SELECT 1", {})]


def test_probe_sql_is_also_capped(configured_timeout: list[int]) -> None:
    connector = _Recorder(supports_timeout=True)

    execute_chat_sql(connector, "SELECT 1", kind="probe SQL")

    assert connector.calls[0][1] == {"timeout_s": DEFAULT_SQL_QUERY_TIMEOUT_SECONDS}


def test_an_explicit_timeout_overrides_the_configured_one(
    configured_timeout: list[int],
) -> None:
    connector = _Recorder(supports_timeout=True)

    execute_chat_sql(connector, "SELECT 1", timeout_s=7)

    assert connector.calls == [("SELECT 1", {"timeout_s": 7})]


class _ReadOnlyRecorder(_Recorder):
    supports_read_only_execution = True


def test_a_write_is_refused_before_it_reaches_the_database() -> None:
    connector = _Recorder(supports_timeout=True)

    with pytest.raises(UnsafeSqlError):
        execute_chat_sql(connector, "DELETE FROM payment")

    assert connector.calls == []


def test_a_refused_statement_is_logged_as_refused_not_as_executed(
    caplog: LogCaptureFixture,
) -> None:
    connector = SimpleNamespace(database_name="pagila", execute=lambda sql: "df")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(UnsafeSqlError):
            execute_chat_sql(connector, "DROP TABLE payment")

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("[pagila] Refused chat SQL") for m in messages)
    assert not any("Executing" in m for m in messages)


def test_the_guard_judges_sql_in_the_connectors_dialect() -> None:
    connector = _Recorder(supports_timeout=False)
    connector.dialect = "tsql"

    assert execute_chat_sql(connector, "SELECT TOP 5 * FROM t") == "df"


def test_read_only_execution_is_requested_when_supported(
    configured_timeout: list[int],
) -> None:
    connector = _ReadOnlyRecorder(supports_timeout=True)

    execute_chat_sql(connector, "SELECT 1")

    assert connector.calls == [
        (
            "SELECT 1",
            {"timeout_s": DEFAULT_SQL_QUERY_TIMEOUT_SECONDS, "read_only": True},
        )
    ]


def test_read_only_execution_is_not_forced_on_connectors_without_it() -> None:
    connector = _Recorder(supports_timeout=False)

    execute_chat_sql(connector, "SELECT 1")

    assert connector.calls == [("SELECT 1", {})]


class _FrameConnector:
    """Connector stub returning a frame of *rows* rows."""

    def __init__(self, rows: int, *, supports_row_limit: bool = False) -> None:
        self.supports_row_limit = supports_row_limit
        self._rows = rows
        self.calls: list[dict] = []

    def execute(self, sql: str, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        return pd.DataFrame({"n": range(self._rows)})


def test_a_result_over_the_limit_is_cut_and_marked(row_limit: list[int]) -> None:
    row_limit.append(5)

    frame = execute_chat_sql(_FrameConnector(8), "SELECT n FROM t")

    assert len(frame) == 5
    assert frame.attrs[TRUNCATED_AT_ATTR] == 5
    assert list(frame["n"]) == [0, 1, 2, 3, 4]


def test_a_result_at_the_limit_is_not_marked(row_limit: list[int]) -> None:
    """Exactly ``limit`` rows is a complete answer, not a cut one."""
    row_limit.append(5)

    frame = execute_chat_sql(_FrameConnector(5), "SELECT n FROM t")

    assert len(frame) == 5
    assert TRUNCATED_AT_ATTR not in frame.attrs


def test_a_connector_with_a_row_limit_is_told_the_limit(row_limit: list[int]) -> None:
    row_limit.append(5)
    connector = _FrameConnector(6, supports_row_limit=True)

    frame = execute_chat_sql(connector, "SELECT n FROM t")

    assert connector.calls == [{"max_rows": 5}]
    # It returns limit + 1 rows so the cut is detectable; we trim and mark.
    assert len(frame) == 5 and frame.attrs[TRUNCATED_AT_ATTR] == 5


def test_a_connector_without_a_row_limit_is_not_given_one() -> None:
    connector = _FrameConnector(3)

    execute_chat_sql(connector, "SELECT n FROM t")

    assert connector.calls == [{}]
