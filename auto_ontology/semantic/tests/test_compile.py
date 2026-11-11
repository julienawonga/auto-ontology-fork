# SPDX-License-Identifier: Apache-2.0

"""A compilation whose embeddings did not land must not return as a success.

Every stage here can complete while the semantic index stays empty: embedding
is best-effort per table, and a pass that finds no table to compile embeds
nothing at all. The verdict therefore comes from comparing the index with the
ontology once the stages are done, and these pin when that comparison is made
and what it does.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_ontology.semantic import compile as compile_module
from auto_ontology.semantic.compile import (
    SemanticIndexIncompleteError,
    run_semantic_compilation,
)
from auto_ontology.semantic.index_health import SemanticIndexHealth


def _patch_stages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    index: SemanticIndexHealth,
    embedder: Any = object(),
    tables: int = 0,
    cancelled: bool = False,
    suggestion_enabled: bool = True,
) -> list[str]:
    """Stub every stage; returns the stages that ran, in order."""
    ran: list[str] = []

    def _stage(name: str, result: int) -> Any:
        def _run(*_args: Any, **_kwargs: Any) -> int:
            ran.append(name)
            return result

        return _run

    def _check(database_name: str) -> SemanticIndexHealth:
        ran.append("index")
        return index

    monkeypatch.setattr(compile_module, "load_domain_summary", lambda _db: None)
    monkeypatch.setattr(
        compile_module, "build_semantic_embedder", lambda _db, reset: embedder
    )
    monkeypatch.setattr(
        compile_module, "compile_semantic_layer", _stage("compile", tables)
    )
    monkeypatch.setattr(compile_module, "is_cancelled", lambda: cancelled)
    monkeypatch.setattr(compile_module, "resolve_semantic_fks", _stage("fks", 0))
    monkeypatch.setattr(
        compile_module,
        "is_sql_attribute_suggestion_enabled",
        lambda: suggestion_enabled,
    )
    monkeypatch.setattr(
        "auto_ontology.semantic.sql_attribute_suggester.suggest_sql_attributes",
        _stage("sql_attributes", 0),
    )
    monkeypatch.setattr(
        compile_module, "build_bridge_tables_sql_attributes", _stage("bridges", 0)
    )
    monkeypatch.setattr(compile_module, "check_semantic_index", _check)
    return ran


def test_a_complete_index_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stages(monkeypatch, index=SemanticIndexHealth("pagila", 80, 80), tables=15)

    assert run_semantic_compilation("pagila") == 15


def test_terms_missing_from_the_index_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident: 15 Terms and 65 attributes compiled, none embedded."""
    ran = _patch_stages(
        monkeypatch, index=SemanticIndexHealth("pagila", 80, 0), tables=15
    )

    with pytest.raises(SemanticIndexIncompleteError, match="missing 80 of 80"):
        run_semantic_compilation("pagila")

    # The verdict comes last: an empty index is no reason to skip the stages
    # that do not depend on it.
    assert ran == ["compile", "fks", "sql_attributes", "bridges", "index"]


def test_query_history_suggestions_are_skipped_when_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreviewed attributes mined from past queries stay out by default."""
    ran = _patch_stages(
        monkeypatch,
        index=SemanticIndexHealth("pagila", 80, 80),
        tables=15,
        suggestion_enabled=False,
    )

    assert run_semantic_compilation("pagila") == 15
    assert ran == ["compile", "fks", "bridges", "index"]


def test_a_pass_that_compiles_nothing_still_answers_for_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tables are compiled once, so every pass after the one whose embeddings
    failed compiles nothing -- and used to succeed on top of the empty index."""
    _patch_stages(monkeypatch, index=SemanticIndexHealth("pagila", 80, 79), tables=0)

    with pytest.raises(SemanticIndexIncompleteError, match="missing 1 of 80"):
        run_semantic_compilation("pagila")


def test_the_index_is_not_judged_when_embedding_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _patch_stages(
        monkeypatch,
        index=SemanticIndexHealth("pagila", 80, 0),
        embedder=None,
        tables=15,
    )

    assert run_semantic_compilation("pagila") == 15
    assert "index" not in ran


def test_a_cancelled_pass_gets_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling stops between tables, so a gap there is not a failure."""
    ran = _patch_stages(
        monkeypatch,
        index=SemanticIndexHealth("pagila", 80, 10),
        tables=3,
        cancelled=True,
    )

    assert run_semantic_compilation("pagila") == 3
    assert ran == ["compile"]
