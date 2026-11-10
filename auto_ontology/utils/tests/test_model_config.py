# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolution of the model-endpoint env var triplets."""

from __future__ import annotations

import pytest

from auto_ontology.utils import model_config

_PREFIXES = ("DEFAULT_MODELS", "REASONING", "NON_REASONING", "EMBED", "RERANK")
_FIELDS = ("ENDPOINT", "API_KEY", "MODEL")
_LEGACY = ("NVIDIA_API_KEY", "BASE_URL", "MODEL_NAME")
_ROLES = ("REASONING", "NON_REASONING", "EMBED", "RERANK")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for prefix in _PREFIXES:
        for field in _FIELDS:
            monkeypatch.delenv(f"{prefix}_{field}", raising=False)
    for name in _LEGACY:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("api_key", ["", "sk-test", "nvapi-test"])
def test_nothing_configured_resolves_to_nothing(
    clean_env: pytest.MonkeyPatch, role: str, field: str, api_key: str
) -> None:
    """No built-in endpoint or model, whatever the key looks like."""
    if api_key:
        clean_env.setenv("DEFAULT_MODELS_API_KEY", api_key)
    expected = api_key if field == "API_KEY" else ""

    assert model_config.resolve(role, field) == expected


def test_the_role_setting_wins_over_the_shared_default(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("DEFAULT_MODELS_MODEL", "shared-model")
    clean_env.setenv("REASONING_MODEL", "some-other-model")

    assert model_config.resolve("REASONING", "MODEL") == "some-other-model"
    assert model_config.resolve("EMBED", "MODEL") == "shared-model"


def test_the_legacy_endpoint_and_model_names_are_still_honored(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("BASE_URL", "https://llm.example/v1")
    clean_env.setenv("MODEL_NAME", "legacy-model")

    assert model_config.resolve("REASONING", "ENDPOINT") == "https://llm.example/v1"
    assert model_config.resolve("RERANK", "MODEL") == "legacy-model"


def test_the_vendor_specific_legacy_key_is_ignored(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("NVIDIA_API_KEY", "nvapi-test")

    assert model_config.resolve("REASONING", "API_KEY") == ""


def test_require_names_every_env_var_that_could_set_the_field(
    clean_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(model_config.ModelConfigError) as caught:
        model_config.require("EMBED", "ENDPOINT")

    assert isinstance(caught.value, EnvironmentError)
    assert "EMBED_ENDPOINT / DEFAULT_MODELS_ENDPOINT / BASE_URL" in str(caught.value)


def test_require_returns_a_configured_value(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DEFAULT_MODELS_API_KEY", "key")

    assert model_config.require("RERANK", "API_KEY") == "key"
    assert model_config.require("RERANK", "MODEL", "explicit") == "explicit"


def test_the_vector_width_defaults_to_2048(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBED_DIMENSIONS", raising=False)

    assert model_config.embed_dimensions() == 2048
    assert model_config.embed_dimensions_requested() is None


def test_the_vector_width_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_DIMENSIONS", " 1536 ")

    assert model_config.embed_dimensions() == 1536
    assert model_config.embed_dimensions_requested() == 1536


@pytest.mark.parametrize("value", ["abc", "0", "-5", "10.5"])
def test_a_bad_vector_width_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EMBED_DIMENSIONS", value)

    with pytest.raises(ValueError, match="EMBED_DIMENSIONS"):
        model_config.embed_dimensions()


@pytest.fixture
def no_extra_fields_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("EMBED_EXTRA_FIELDS", raising=False)
    monkeypatch.delenv("EMBED_NIM_FIELDS", raising=False)
    return monkeypatch


_SWITCH_VALUES = [
    ("", True),
    ("true", True),
    ("1", True),
    ("false", False),
    ("OFF", False),
    ("0", False),
]


@pytest.mark.parametrize(("value", "enabled"), _SWITCH_VALUES)
@pytest.mark.parametrize("name", ["EMBED_EXTRA_FIELDS", "EMBED_NIM_FIELDS"])
def test_the_extra_fields_switch(
    no_extra_fields_env: pytest.MonkeyPatch, name: str, value: str, enabled: bool
) -> None:
    """On by default; the older ``EMBED_NIM_FIELDS`` name is still accepted."""
    no_extra_fields_env.setenv(name, value)

    assert model_config.embed_extra_fields_enabled() is enabled


def test_the_new_switch_name_wins(no_extra_fields_env: pytest.MonkeyPatch) -> None:
    no_extra_fields_env.setenv("EMBED_NIM_FIELDS", "false")
    no_extra_fields_env.setenv("EMBED_EXTRA_FIELDS", "true")

    assert model_config.embed_extra_fields_enabled() is True
