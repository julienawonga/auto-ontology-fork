# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pure helpers in auto_ontology.semantic.embed."""

from __future__ import annotations

from typing import Any

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
