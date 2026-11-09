# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regex allow/deny filtering of relations at catalog-extraction time.

Why this exists: a warehouse routinely exposes relations that are listed in its
metadata but cannot actually be read — a Distributed table whose shard-local
table has drifted, a Kafka-engine queue, a staging table nobody should query.
Metadata succeeds on all of them, so they land in the catalog, retrieval offers
them, and every generated query against them fails. Naming them is the only
thing the operator can do; these two patterns are how.

Applied in :func:`auto_ontology.catalog.extract.create_dataframe`, which is the
one place every connector's relations pass through, so filtering is uniform
rather than per-connector the way the ``schemas`` allowlist has to be.

Semantics, kept deliberately small:

``table_deny_regex``
    Drop any relation whose name matches.
``table_allow_regex``
    When set, keep *only* relations whose name matches.
Deny wins
    A relation matching both is dropped. The narrower rule is the safer one,
    and it makes "allow this family, minus these two" expressible.

Views are excluded by type rather than by name: ``exclude_views`` drops every
view and materialized view, which is what an operator wants when the views only
re-present tables that are already catalogued (and would otherwise show up as a
second, near-identical Term for each one).

Matching is :func:`re.search` against the **unqualified** relation name, so
``_stage`` matches ``pg_perfbot_flat_all_stage`` without anchors — grep-like,
which is what an operator typing a pattern into a form expects. Anchor with
``^``/``$`` for exact matches. Matching is case-sensitive, because the engines
we target treat identifiers that way.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from auto_ontology.catalog.constants import TableTypes

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

logger = logging.getLogger(__name__)

ALLOW_KEY = "table_allow_regex"
DENY_KEY = "table_deny_regex"
EXCLUDE_VIEWS_KEY = "exclude_views"

#: Connection keys this module reads, for callers that persist or forward them.
FILTER_KEYS = (ALLOW_KEY, DENY_KEY, EXCLUDE_VIEWS_KEY)

_VIEW_TYPES = frozenset({TableTypes.VIEW, TableTypes.MATERIALIZED_VIEW})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def parse_flag(raw: Any) -> bool:
    """Read a boolean connection setting, tolerating the string forms.

    A connection can reach here from a form (``True``), from JSON read back from
    the store, or from an env-supplied string. Anything not clearly true is off:
    the safe reading of a setting whose absence means "ingest everything".
    """
    if isinstance(raw, bool):
        return raw
    return str(raw if raw is not None else "").strip().lower() in _TRUTHY


class InvalidTableFilterError(ValueError):
    """Raised when an allow/deny pattern is not a valid regular expression."""


def compile_pattern(raw: Any, *, field: str) -> "re.Pattern[str] | None":
    """Compile one pattern, or ``None`` when it is absent/blank.

    Raises :class:`InvalidTableFilterError` rather than returning ``None`` on a
    bad pattern. Silently ignoring it would ingest everything the operator was
    trying to exclude, which is the opposite of what they asked for and is
    invisible until a query fails much later.
    """
    if raw is None:
        return None
    pattern = str(raw).strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise InvalidTableFilterError(f"{field} is not a valid regex: {exc}") from exc


@dataclass(frozen=True)
class TableFilter:
    """An allow/deny pair on relation names, plus an optional views switch."""

    allow: "re.Pattern[str] | None" = None
    deny: "re.Pattern[str] | None" = None
    exclude_views: bool = False

    @property
    def active(self) -> bool:
        """Whether this filter would exclude anything at all."""
        return self.allow is not None or self.deny is not None or self.exclude_views

    def keep(self, table_name: str) -> bool:
        """Whether *table_name* survives the filter."""
        if not table_name:
            return True
        if self.deny is not None and self.deny.search(table_name):
            return False
        if self.allow is not None and not self.allow.search(table_name):
            return False
        return True

    def describe(self) -> str:
        """Short human-readable form, for log lines."""
        parts = []
        if self.allow is not None:
            parts.append(f"allow={self.allow.pattern!r}")
        if self.deny is not None:
            parts.append(f"deny={self.deny.pattern!r}")
        if self.exclude_views:
            parts.append("exclude_views")
        return ", ".join(parts) or "inactive"


def from_connection(connection: dict[str, Any] | None) -> TableFilter:
    """Build a :class:`TableFilter` from a connection dict.

    Returns an inactive filter when no key is set, so callers can apply it
    unconditionally without branching.
    """
    if not connection:
        return TableFilter()
    return TableFilter(
        allow=compile_pattern(connection.get(ALLOW_KEY), field="Table allowlist"),
        deny=compile_pattern(connection.get(DENY_KEY), field="Table denylist"),
        exclude_views=parse_flag(connection.get(EXCLUDE_VIEWS_KEY)),
    )


def filter_frame(
    frame: "pd.DataFrame | None", table_filter: TableFilter
) -> "pd.DataFrame | None":
    """Drop rows whose ``table_name`` the filter excludes.

    A frame without a ``table_name`` column is returned untouched — ``queries``
    and other non-relation frames legitimately lack one.
    """
    if frame is None or not table_filter.active:
        return frame
    if getattr(frame, "empty", True):
        return frame
    if "table_name" not in frame.columns:
        return frame
    keep = frame["table_name"].map(
        lambda name: table_filter.keep("" if name is None else str(name))
    )
    return frame[keep]


def _key(schema: Any, name: Any) -> tuple[str, str]:
    return ("" if schema is None else str(schema), "" if name is None else str(name))


def _view_keys(
    tables: "pd.DataFrame | None", views: "pd.DataFrame | None"
) -> set[tuple[str, str]]:
    """``(schema, name)`` of every view, from both places a connector reports one.

    A connector states a relation's type in ``tables.table_type`` when it can,
    and lists views in ``get_views()``. Neither is complete alone: Postgres'
    ``get_views()`` is ``information_schema.views``, which omits materialized
    views, and a connector without ``table_type`` reports every relation as a
    base table. The union misses only a view that neither one admits to.
    """
    keys: set[tuple[str, str]] = set()
    if tables is not None and not tables.empty and "table_name" in tables.columns:
        types = (
            tables["table_type"].astype(str).str.lower()
            if "table_type" in tables.columns
            else None
        )
        schemas = tables["table_schema"] if "table_schema" in tables.columns else None
        for i, name in enumerate(tables["table_name"]):
            if types is not None and types.iloc[i] in _VIEW_TYPES:
                keys.add(_key(None if schemas is None else schemas.iloc[i], name))
    if views is not None and not views.empty and "table_name" in views.columns:
        schemas = views["table_schema"] if "table_schema" in views.columns else None
        for i, name in enumerate(views["table_name"]):
            keys.add(_key(None if schemas is None else schemas.iloc[i], name))
    return keys


def _without_views(
    frame: "pd.DataFrame | None", keys: set[tuple[str, str]]
) -> "pd.DataFrame | None":
    """*frame* minus the rows that belong to a view in *keys*.

    Matched on ``(schema, name)`` when the frame carries a schema, so a view and
    an unrelated base table that share a name in different schemas are told
    apart; on name alone when it does not.
    """
    if frame is None or getattr(frame, "empty", True) or not keys:
        return frame
    if "table_name" not in frame.columns:
        return frame
    names = {name for _, name in keys}
    if "table_schema" in frame.columns:
        drop = [
            _key(schema, name) in keys
            for schema, name in zip(frame["table_schema"], frame["table_name"])
        ]
    else:
        drop = [str(name) in names for name in frame["table_name"]]
    return frame[[not d for d in drop]]


def apply(
    tables: "pd.DataFrame | None",
    columns: "pd.DataFrame | None",
    views: "pd.DataFrame | None",
    table_filter: TableFilter,
) -> tuple["pd.DataFrame | None", "pd.DataFrame | None", "pd.DataFrame | None"]:
    """Filter the three relation frames together.

    All three must be filtered as one: ``create_dataframe`` validates that every
    listed relation has described columns, so dropping a table while keeping its
    columns (or the reverse) would trip that check.
    """
    if not table_filter.active:
        return tables, columns, views

    before = 0 if tables is None or tables.empty else len(tables)
    if table_filter.exclude_views:
        keys = _view_keys(tables, views)
        tables = _without_views(tables, keys)
        columns = _without_views(columns, keys)
        views = _without_views(views, keys)
    tables = filter_frame(tables, table_filter)
    columns = filter_frame(columns, table_filter)
    views = filter_frame(views, table_filter)
    after = 0 if tables is None or tables.empty else len(tables)

    if before != after:
        logger.info(
            "Table filter (%s) kept %d of %d table(s)",
            table_filter.describe(),
            after,
            before,
        )
    return tables, columns, views
