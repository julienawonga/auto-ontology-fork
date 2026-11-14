# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exploration service layer.

Keeps the HTTP router independent from the DAL and provides the
domain boundary for future Exploration-specific orchestration.
"""

from __future__ import annotations

from typing import Any

from auto_ontology.dal import connections, domains, terms

from auto_ontology.dal.exploration import (
    MAX_EXPLORATION_GRAPH_NODES,
    fetch_column_attribute_exploration_details,
    fetch_column_exploration_details,
    fetch_data_exploration_edges,
    fetch_data_exploration_graph,
    fetch_exploration_related_nodes,
    fetch_semantic_database_overview,
    fetch_semantic_exploration_graph,
    fetch_semantic_link_path,
    fetch_sql_attribute_exploration_details,
    fetch_sql_exploration_details,
    fetch_table_exploration_details,
    fetch_table_zones_map,
    fetch_term_exploration_details,
)
from auto_ontology.retrieval.domains import group_terms


class UnknownDatabase(ValueError):
    """The requested catalog database does not exist."""


class UnknownDomain(ValueError):
    """The requested business domain does not exist."""


class DuplicateDomainName(ValueError):
    """A business domain already uses the requested name in this database."""


def fetch_semantic_domains(database_id: str) -> dict[str, Any]:
    """Return business domains and their links for one database."""
    from auto_ontology.dal.exploration import fetch_semantic_domains as fetch

    if terms.resolve_database(database_id) is None:
        raise UnknownDatabase(database_id)
    return fetch(database_id)


def generate_semantic_domains(database_id: str) -> dict[str, Any]:
    """Group a database's Terms, persist the generated domains, and return them."""
    database = terms.resolve_database(database_id)
    if database is None:
        raise UnknownDatabase(database_id)
    graph = fetch_semantic_exploration_graph(
        zone_ids=None, limit=MAX_EXPLORATION_GRAPH_NODES, database_id=database_id
    )
    llm: Any = None
    try:
        from auto_ontology.utils.llm_invoke import get_llm_client

        llm = get_llm_client(temperature=0.0)
    except Exception:  # noqa: BLE001 - grouping has a deterministic fallback
        pass
    groups = group_terms(
        graph.get("nodes", []),
        connections.get_ontology_language(database["name"]),
        llm,
    )
    domains.replace_auto_domains(database["id"], groups)
    return fetch_semantic_domains(database["id"])


def rename_semantic_domain(
    domain_id: str, name: str, description: str | None
) -> dict[str, Any]:
    """Rename a domain after checking its database-scoped name uniqueness."""
    current = next((row for row in _all_domains() if row["id"] == domain_id), None)
    if current is None:
        raise UnknownDomain(domain_id)
    clean_name = name.strip()
    if any(
        row["id"] != domain_id
        and row["database_id"] == current["database_id"]
        and row["name"].strip().casefold() == clean_name.casefold()
        for row in _all_domains()
    ):
        raise DuplicateDomainName(clean_name)
    if not domains.rename_domain(domain_id, clean_name, description):
        raise UnknownDomain(domain_id)
    return fetch_semantic_domains(current["database_id"])


def move_semantic_domain_terms(domain_id: str, term_ids: list[str]) -> dict[str, Any]:
    """Move Terms to a domain and return the refreshed database domain map."""
    current = next((row for row in _all_domains() if row["id"] == domain_id), None)
    if current is None:
        raise UnknownDomain(domain_id)
    try:
        if not domains.move_terms(domain_id, term_ids):
            raise UnknownDomain(domain_id)
    except ValueError as exc:
        raise ValueError("Unknown Term ids") from exc
    return fetch_semantic_domains(current["database_id"])


def _all_domains() -> list[dict[str, Any]]:
    """Return stored domains across databases for id and uniqueness checks."""
    from auto_ontology.dal import schema as s
    from auto_ontology.dal.session import store
    from sqlalchemy import select

    return [dict(row) for row in store().query_read(select(s.domain))]


__all__ = [
    "MAX_EXPLORATION_GRAPH_NODES",
    "fetch_column_attribute_exploration_details",
    "fetch_column_exploration_details",
    "fetch_data_exploration_edges",
    "fetch_data_exploration_graph",
    "fetch_exploration_related_nodes",
    "fetch_semantic_database_overview",
    "fetch_semantic_domains",
    "generate_semantic_domains",
    "rename_semantic_domain",
    "move_semantic_domain_terms",
    "fetch_semantic_exploration_graph",
    "fetch_semantic_link_path",
    "fetch_sql_attribute_exploration_details",
    "fetch_sql_exploration_details",
    "fetch_table_exploration_details",
    "fetch_table_zones_map",
    "fetch_term_exploration_details",
]
