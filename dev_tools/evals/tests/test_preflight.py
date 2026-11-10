# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import httpx
import pytest

from dev_tools.evals.preflight import (
    EMPTY,
    INCOMPLETE,
    POPULATED,
    PreflightError,
    entry_state,
    index_state,
    preflight,
)

URL = "http://backend:3001"


def _entry(name: str, expected: int, indexed: int) -> dict[str, Any]:
    return {
        "database_name": name,
        "expected": expected,
        "indexed": indexed,
        "missing": expected - indexed,
    }


def _client(status: int = 200, body: Any = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/semantic-compilation/status"
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _status(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"calculated": True, "running": False, "index": list(entries)}


def test_entry_states() -> None:
    assert entry_state(_entry("a", 80, 80)) == POPULATED
    assert entry_state(_entry("a", 80, 79)) == INCOMPLETE
    assert entry_state(_entry("a", 80, 0)) == EMPTY
    assert entry_state(_entry("a", 0, 0)) == EMPTY  # no ontology at all


def test_index_state_of_several_databases() -> None:
    assert index_state([_entry("a", 4, 4), _entry("b", 2, 2)]) == POPULATED
    assert index_state([_entry("a", 4, 0), _entry("b", 2, 0)]) == EMPTY
    assert index_state([_entry("a", 4, 4), _entry("b", 2, 0)]) == INCOMPLETE


def test_a_populated_index_passes_and_is_recorded() -> None:
    with _client(body=_status(_entry("pagila", 80, 80), _entry("chinook", 5, 0))) as c:
        meta = preflight(URL, "pagila", client=c)
    assert meta["backend_url"] == URL
    # only the database under test is recorded and judged
    assert meta["ontology_index"] == [
        {"database_name": "pagila", "expected": 80, "indexed": 80, "missing": 0}
    ]
    assert "allow_empty_index" not in meta


def test_an_empty_index_refuses_to_run() -> None:
    with _client(body=_status(_entry("pagila", 80, 0))) as c:
        with pytest.raises(PreflightError, match="pagila: index is empty") as info:
            preflight(URL, "pagila", client=c)
    assert "--allow-empty-index" in str(info.value)


def test_a_partial_index_refuses_to_run() -> None:
    with _client(body=_status(_entry("pagila", 80, 70))) as c:
        with pytest.raises(PreflightError, match="index is incomplete"):
            preflight(URL, "pagila", client=c)


def test_the_override_runs_and_records_that_it_did() -> None:
    with _client(body=_status(_entry("pagila", 80, 0))) as c:
        meta = preflight(URL, "pagila", allow_empty_index=True, client=c)
    assert meta["allow_empty_index"] is True
    assert meta["ontology_index"][0]["indexed"] == 0
    assert "empty" in meta["ontology_index_problems"][0]


def test_without_a_target_every_database_counts() -> None:
    with _client(body=_status(_entry("a", 4, 4), _entry("b", 2, 0))) as c:
        with pytest.raises(PreflightError, match="b: index is empty"):
            preflight(URL, None, client=c)


def test_an_unknown_target_refuses_and_names_the_known_ones() -> None:
    with _client(body=_status(_entry("pagila", 80, 80))) as c:
        with pytest.raises(PreflightError, match="not in the semantic index") as info:
            preflight(URL, "chinook", client=c)
    assert "pagila" in str(info.value)


def test_a_backend_that_omits_the_index_is_reported_and_refused() -> None:
    with _client(body={"calculated": True, "running": False}) as c:
        with pytest.raises(PreflightError, match="did not report"):
            preflight(URL, "pagila", client=c)
    with _client(body={"calculated": True, "index": None}) as c:
        meta = preflight(URL, "pagila", allow_empty_index=True, client=c)
    assert "ontology_index" not in meta
    assert meta["ontology_index_error"]


def test_an_unreachable_status_endpoint_refuses() -> None:
    with _client(status=500, body={"detail": "boom"}) as c:
        with pytest.raises(PreflightError, match="cannot read"):
            preflight(URL, "pagila", client=c)
