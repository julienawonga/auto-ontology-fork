# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pure helpers in auto_ontology.semantic.embed."""

from __future__ import annotations

from typing import Any, cast

import pytest

from auto_ontology.semantic import embed, semantic_fk
from auto_ontology.semantic.embed import _format_sample_values
from auto_ontology.utils import embedding


def test_format_sample_values_handles_legacy_json_string_and_native_list() -> None:
    # Legacy Column nodes still store sample_values as a JSON string.
    assert _format_sample_values('["a", "b"]') == " Sample values: a, b."
    # Current writers store a native list.
    assert _format_sample_values(["a", "b"]) == " Sample values: a, b."


def test_format_sample_values_filters_long_values_and_handles_empty() -> None:
    long_value = "x" * 31
    assert _format_sample_values([long_value, "ok"]) == " Sample values: ok."
    assert _format_sample_values(None) == ""
    assert _format_sample_values([]) == ""
    assert _format_sample_values(["a", None, "b"]) == " Sample values: a, b."
    assert _format_sample_values('["a", null, "b"]') == " Sample values: a, b."


@pytest.fixture
def configured_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint that rejects NVIDIA-only request fields, 768 wide."""
    monkeypatch.setattr(embedding, "_EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_EXTRA_FIELDS", "false")
    monkeypatch.setenv("EMBED_DIMENSIONS", "768")


def test_semantic_embedder_honours_the_embedding_environment(
    monkeypatch: pytest.MonkeyPatch, configured_embedding: None
) -> None:
    """Spelling the parameters out dropped these, so the endpoint got
    ``input_type: passage``, rejected every Term and left the index empty."""
    monkeypatch.setattr(embed, "get_semantic_vdb", lambda **_kw: object())

    embedder = embed.build_semantic_embedder("pagila", reset=False)

    assert embedder is not None
    assert embedder.embed_params.extra_fields is False
    assert embedder.embed_params.expected_dimensions == 768


def test_semantic_embedder_is_disabled_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding, "_EMBED_API_KEY", "")
    monkeypatch.setattr(embed, "get_semantic_vdb", lambda **_kw: object())

    assert embed.build_semantic_embedder("pagila", reset=False) is None


def test_semantic_fk_retriever_uses_the_same_embedding_parameters(
    monkeypatch: pytest.MonkeyPatch, configured_embedding: None
) -> None:
    built: list[tuple[Any, Any]] = []
    monkeypatch.setattr(semantic_fk, "get_semantic_vdb", lambda **_kw: "vdb")
    monkeypatch.setattr(
        semantic_fk, "Retriever", lambda vdb, params: built.append((vdb, params))
    )

    semantic_fk._build_retriever("pagila")

    ((_, params),) = built
    assert params.extra_fields is False
    assert params.expected_dimensions == 768


# ---------------------------------------------------------------------------
# Rebuilding the index from the stored Terms
# ---------------------------------------------------------------------------


class _Vdb:
    """Records what a rebuild deletes and writes."""

    def __init__(self) -> None:
        self.deleted: list[tuple[list[str], str]] = []
        self.written: list[dict[str, Any]] = []

    def delete_by_ids(self, node_ids: list[str], database_name: str) -> int:
        self.deleted.append((sorted(node_ids), database_name))
        return len(node_ids)

    def run(self, records: list[list[dict[str, Any]]]) -> int:
        self.written.extend(records[0])
        return len(records[0])


def _rebuild(
    monkeypatch: pytest.MonkeyPatch,
    terms: list[dict[str, Any]],
    attrs: list[dict[str, Any]],
    *,
    rejected: frozenset[str] = frozenset(),
) -> tuple[embed.SemanticIndexRebuild, _Vdb]:
    """Run a rebuild of ``pagila`` whose endpoint embeds all but *rejected* ids."""
    import pandas as pd

    from auto_ontology.dal import terms as terms_dal

    def _fetch(database_name: str) -> tuple[list[dict], list[dict]]:
        assert database_name == "pagila"
        return terms, attrs

    def _batch_embed(rows: list[dict], _params: object) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    **row,
                    "metadata": {
                        **row["metadata"],
                        **(
                            {}
                            if row["metadata"]["id"] in rejected
                            else {"embedding": [0.1, 0.2]}
                        ),
                    },
                }
                for row in rows
            ]
        )

    monkeypatch.setattr(terms_dal, "fetch_terms_and_attributes_for_database", _fetch)
    monkeypatch.setattr(embed, "batch_embed", _batch_embed)
    vdb = _Vdb()
    embedder = embed.SemanticEmbedder(
        database_name="pagila", embed_params=cast(Any, object()), vdb=cast(Any, vdb)
    )
    return embed.embed_all_semantic_nodes(embedder), vdb


_FILM = {"id": "t-film", "name": "Film", "description": "A film", "synonyms": []}
_TITLE = {
    "id": "a-title",
    "name": "Title",
    "term_name": "Film",
    "source_column": "title",
}
_LENGTH = {
    "id": "a-length",
    "name": "Length",
    "term_name": "Film",
    "source_column": "length",
}


def test_rebuild_writes_every_term_and_attribute_of_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, vdb = _rebuild(monkeypatch, [_FILM], [_TITLE, _LENGTH])

    assert result == embed.SemanticIndexRebuild(expected=3, written=3)
    assert {r["metadata"]["id"] for r in vdb.written} == {
        "t-film",
        "a-title",
        "a-length",
    }
    assert {r["metadata"]["database_name"] for r in vdb.written} == {"pagila"}


def test_rebuild_replaces_rows_instead_of_adding_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store only appends; without the delete a second run doubles the index."""
    _result, vdb = _rebuild(monkeypatch, [_FILM], [_TITLE])

    assert vdb.deleted == [(["a-title", "t-film"], "pagila")]


def test_rebuild_reports_a_partial_failure_and_keeps_what_it_could_not_redo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, vdb = _rebuild(
        monkeypatch, [_FILM], [_TITLE, _LENGTH], rejected=frozenset({"a-length"})
    )

    assert result == embed.SemanticIndexRebuild(expected=3, written=2)
    # The row that got no embedding is not deleted: whatever is stored for it
    # still answers searches, and nothing would be written in its place.
    assert vdb.deleted == [(["a-title", "t-film"], "pagila")]


def test_rebuild_against_a_dead_endpoint_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the incident's endpoint did: every request rejected."""
    result, vdb = _rebuild(
        monkeypatch,
        [_FILM],
        [_TITLE],
        rejected=frozenset({"t-film", "a-title"}),
    )

    assert result == embed.SemanticIndexRebuild(expected=2, written=0)
    assert vdb.deleted == [] and vdb.written == []


def test_rebuild_embeds_attributes_whose_term_is_not_in_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The health check expects every attribute; one never embedded would be a
    gap no rebuild closes."""
    stray = {**_TITLE, "id": "a-stray", "term_name": "Elsewhere"}

    result, vdb = _rebuild(monkeypatch, [_FILM], [stray])

    assert result == embed.SemanticIndexRebuild(expected=2, written=2)
    assert {r["metadata"]["id"] for r in vdb.written} == {"t-film", "a-stray"}


def test_rebuild_of_a_database_without_terms_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, vdb = _rebuild(monkeypatch, [], [])

    assert result == embed.SemanticIndexRebuild(expected=0, written=0)
    assert vdb.deleted == [] and vdb.written == []
