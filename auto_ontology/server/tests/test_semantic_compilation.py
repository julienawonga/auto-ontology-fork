# SPDX-License-Identifier: Apache-2.0

"""The semantic index rebuild route and the drift report on the status route."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from auto_ontology.semantic.embed import SemanticIndexRebuild
from auto_ontology.semantic.index_health import SemanticIndexHealth
from auto_ontology.semantic.value_index import ValueIndexRebuild
from auto_ontology.server.semantic_compilation import router


@pytest.fixture
def rebuild(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """A catalog holding ``pagila``, idle, with an endpoint that embeds 78/80.

    Returns the ``(database_name, reset)`` each embedder was built with.
    """
    built: list[tuple[str, bool]] = []

    def _build(database_name: str, *, reset: bool) -> Any:
        built.append((database_name, reset))
        return object()

    monkeypatch.setattr(router, "fetch_databases", lambda: [{"name": "pagila"}])
    monkeypatch.setattr(router, "is_semantic_compilation_running", lambda: False)
    monkeypatch.setattr(router, "build_semantic_embedder", _build)
    monkeypatch.setattr(
        router,
        "embed_all_semantic_nodes",
        lambda _embedder: SemanticIndexRebuild(expected=80, written=78),
    )
    return built


def test_reindex_reports_written_against_expected(
    rebuild: list[tuple[str, bool]],
) -> None:
    assert router.reindex("pagila") == {
        "database_name": "pagila",
        "expected": 80,
        "written": 78,
    }


def test_reindex_never_resets_the_index(rebuild: list[tuple[str, bool]]) -> None:
    """``reset=True`` deletes every row of the database first -- including the
    SqlAttributes and analyses this route does not rebuild."""
    router.reindex("pagila")

    assert rebuild == [("pagila", False)]


def test_reindex_of_an_unknown_database_is_a_404(
    rebuild: list[tuple[str, bool]],
) -> None:
    with pytest.raises(HTTPException) as raised:
        router.reindex("nope")

    assert raised.value.status_code == 404
    assert rebuild == []


def test_reindex_is_refused_while_compilation_is_running(
    monkeypatch: pytest.MonkeyPatch, rebuild: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(router, "is_semantic_compilation_running", lambda: True)

    with pytest.raises(HTTPException) as raised:
        router.reindex("pagila")

    assert raised.value.status_code == 409
    assert rebuild == []


def test_reindex_without_an_embedding_endpoint_is_a_503(
    monkeypatch: pytest.MonkeyPatch, rebuild: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(router, "build_semantic_embedder", lambda _db, *, reset: None)

    with pytest.raises(HTTPException) as raised:
        router.reindex("pagila")

    assert raised.value.status_code == 503


def _patch_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "semantic_layer_calculated", lambda: True)
    monkeypatch.setattr(router, "is_semantic_compilation_running", lambda: False)
    monkeypatch.setattr(router, "get_last_successful_run", lambda: None)
    monkeypatch.setattr(router, "get_last_failure_if_most_recent", lambda: None)


def test_status_reports_the_index_gap_per_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_status(monkeypatch)
    monkeypatch.setattr(
        router,
        "check_semantic_indexes",
        lambda: [SemanticIndexHealth("pagila", expected=80, indexed=0)],
    )

    assert router.semantic_status()["index"] == [
        {"database_name": "pagila", "expected": 80, "indexed": 0, "missing": 80}
    ]


def test_status_still_answers_when_the_index_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat page gates on ``calculated``; an unreadable vector store must
    cost it the drift report and nothing else."""
    _patch_status(monkeypatch)

    def _unreachable() -> list[SemanticIndexHealth]:
        raise RuntimeError("vdb unreachable")

    monkeypatch.setattr(router, "check_semantic_indexes", _unreachable)

    status = router.semantic_status()

    assert status["calculated"] is True
    assert status["index"] is None


@pytest.fixture
def value_build(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """A catalog holding ``pagila``, idle, with a connector and an endpoint.

    Returns the ``(database_name, reset)`` each embedder was built with.
    """
    built: list[tuple[str, bool]] = []

    class _Connector:
        database_name = "Pagila"

    def _build(database_name: str, *, reset: bool) -> Any:
        built.append((database_name, reset))
        return object()

    monkeypatch.setattr(router, "fetch_databases", lambda: [{"name": "pagila"}])
    monkeypatch.setattr(router, "is_semantic_compilation_running", lambda: False)
    monkeypatch.setattr(router, "get_connectors", lambda: [_Connector()])
    monkeypatch.setattr(router, "build_semantic_embedder", _build)
    monkeypatch.setattr(
        router,
        "embed_column_values",
        lambda _embedder, _connector: ValueIndexRebuild(
            expected=120, written=118, columns_indexed=6, columns_skipped=70
        ),
    )
    return built


def test_value_index_reports_what_it_wrote_and_skipped(
    value_build: list[tuple[str, bool]],
) -> None:
    assert router.build_value_index("pagila") == {
        "database_name": "pagila",
        "expected": 120,
        "written": 118,
        "columns_indexed": 6,
        "columns_skipped": 70,
    }


def test_value_index_never_resets_the_index(
    value_build: list[tuple[str, bool]],
) -> None:
    router.build_value_index("pagila")

    assert value_build == [("pagila", False)]


def test_value_index_of_an_unknown_database_is_a_404(
    value_build: list[tuple[str, bool]],
) -> None:
    with pytest.raises(HTTPException) as raised:
        router.build_value_index("nope")

    assert raised.value.status_code == 404
    assert value_build == []


def test_value_index_is_refused_while_compilation_is_running(
    monkeypatch: pytest.MonkeyPatch, value_build: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(router, "is_semantic_compilation_running", lambda: True)

    with pytest.raises(HTTPException) as raised:
        router.build_value_index("pagila")

    assert raised.value.status_code == 409
    assert value_build == []


def test_value_index_without_an_embedding_endpoint_is_a_503(
    monkeypatch: pytest.MonkeyPatch, value_build: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(router, "build_semantic_embedder", lambda _db, *, reset: None)

    with pytest.raises(HTTPException) as raised:
        router.build_value_index("pagila")

    assert raised.value.status_code == 503


def test_value_index_without_a_connector_is_a_503(
    monkeypatch: pytest.MonkeyPatch, value_build: list[tuple[str, bool]]
) -> None:
    monkeypatch.setattr(router, "get_connectors", lambda: [])

    with pytest.raises(HTTPException) as raised:
        router.build_value_index("pagila")

    assert raised.value.status_code == 503
    assert value_build == []
