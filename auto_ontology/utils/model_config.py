# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared resolution of model-endpoint env var triplets.

Every triplet (``REASONING``, ``NON_REASONING``, ``EMBED``, ``RERANK``) exposes
three fields — ``ENDPOINT`` (URL), ``API_KEY``, and ``MODEL`` — as
``<PREFIX>_<FIELD>`` env vars. Any field left unset falls back to the
corresponding ``DEFAULT_MODELS_<FIELD>``, so a single ``DEFAULT_MODELS_*`` triplet
can supply the shared endpoint/key/model for all of them. ``ENDPOINT`` and
``MODEL`` also fall back to their legacy pre-triplet names (``BASE_URL`` /
``MODEL_NAME``) for backward compatibility.

There is no built-in endpoint or model: a field nobody configured resolves to
``""``, and the client that needs it refuses to start with an error naming the
env vars to set (see :func:`require`), rather than silently calling some
provider's hosted service.
"""

from __future__ import annotations

import os

# Legacy (pre-triplet) env var names, still honored so existing deployments with
# BASE_URL / MODEL_NAME configs keep working.
_LEGACY_ENV: dict[str, str] = {
    "ENDPOINT": "BASE_URL",
    "MODEL": "MODEL_NAME",
}


def _default(field: str) -> str:
    """Shared default for *field*: ``DEFAULT_MODELS_<field>`` then the legacy name."""
    value = os.environ.get(f"DEFAULT_MODELS_{field}", "")
    if not value and field in _LEGACY_ENV:
        value = os.environ.get(_LEGACY_ENV[field], "")
    return value


def resolve(prefix: str, field: str) -> str:
    """Resolve one triplet field, or ``""`` when it is not configured.

    Order of precedence: the ``<prefix>_<field>`` env var, then the shared
    ``DEFAULT_MODELS_<field>``, then the legacy name for that field.
    """
    return os.environ.get(f"{prefix}_{field}", "") or _default(field)


def env_names(prefix: str, field: str) -> str:
    """The env vars that configure ``<prefix>_<field>``, for error messages."""
    names = [f"{prefix}_{field}", f"DEFAULT_MODELS_{field}"]
    if field in _LEGACY_ENV:
        names.append(_LEGACY_ENV[field])
    return " / ".join(names)


class ModelConfigError(EnvironmentError):
    """A model endpoint, key or model name the caller needs is not configured."""


def require(prefix: str, field: str, value: str | None = None) -> str:
    """*value* (default: the resolved field), or raise naming the env vars to set."""
    resolved = resolve(prefix, field) if value is None else value
    if not resolved:
        raise ModelConfigError(
            f"{prefix} {field.lower().replace('_', ' ')} is not configured: "
            f"set {env_names(prefix, field)}"
        )
    return resolved


# Width of the vectors in the store. 2048 is kept as the default so existing
# stores keep their column width; set ``EMBED_DIMENSIONS`` to match your model.
DEFAULT_EMBED_DIMENSIONS = 2048


def _dimensions_from_env() -> int | None:
    raw = os.environ.get("EMBED_DIMENSIONS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"EMBED_DIMENSIONS must be a positive integer, got {raw!r}")
    return value


def embed_dimensions() -> int:
    """Width of the stored vectors: ``EMBED_DIMENSIONS``, else 2048."""
    return _dimensions_from_env() or DEFAULT_EMBED_DIMENSIONS


def embed_dimensions_requested() -> int | None:
    """``EMBED_DIMENSIONS`` when set, to send as ``dimensions``; ``None`` otherwise.

    Left unset, no ``dimensions`` field goes out, exactly as before.
    """
    return _dimensions_from_env()


_FALSE_VALUES = {"0", "false", "no", "off"}


def embed_extra_fields_enabled() -> bool:
    """Whether embedding requests carry the extra body fields.

    The fields are ``input_type``, ``truncate`` and ``modality``, which
    asymmetric embedding servers use. On by default. ``EMBED_EXTRA_FIELDS=false``
    drops them for endpoints that reject unknown fields; the older name
    ``EMBED_NIM_FIELDS`` is still accepted, and ``EMBED_EXTRA_FIELDS`` wins when
    both are set.
    """
    raw = os.environ.get("EMBED_EXTRA_FIELDS", "").strip()
    if not raw:
        raw = os.environ.get("EMBED_NIM_FIELDS", "").strip()
    return raw.lower() not in _FALSE_VALUES
