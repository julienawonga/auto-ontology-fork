# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, database-free clustering for semantic Exploration Terms."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any


def cluster_terms(
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    min_terms: int = 30,
) -> dict[str, list[dict[str, Any]]]:
    """Group connected Terms into deterministic, readable semantic domains."""
    ordered_nodes = sorted(nodes, key=lambda node: str(node["id"]))
    ids = [str(node["id"]) for node in ordered_nodes]
    node_by_id = {str(node["id"]): node for node in ordered_nodes}
    edge_weights: dict[tuple[str, str], int] = defaultdict(int)
    for link in links:
        source, target = str(link["source"]), str(link["target"])
        if source in node_by_id and target in node_by_id and source != target:
            edge_weights[tuple(sorted((source, target)))] += 1

    def result_for(groups: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
        groups = [sorted(group) for group in groups if group]
        domains: list[dict[str, Any]] = []
        domain_for: dict[str, str] = {}
        for members in sorted(groups, key=lambda group: group[0]):
            domain_id = hashlib.sha1("\n".join(members).encode()).hexdigest()[:12]
            # A later improvement can add an LLM naming step; v1 uses an observed Term name.
            best = min(
                members,
                key=lambda term_id: (
                    -int(node_by_id[term_id].get("relationship_count") or 0),
                    str(node_by_id[term_id].get("name") or "").casefold(),
                    term_id,
                ),
            )
            domains.append(
                {
                    "id": domain_id,
                    "name": str(node_by_id[best].get("name") or ""),
                    "term_ids": members,
                    "term_count": len(members),
                }
            )
            domain_for.update({term_id: domain_id for term_id in members})
        domain_weights: dict[tuple[str, str], int] = defaultdict(int)
        for (source, target), weight in sorted(edge_weights.items()):
            left, right = domain_for[source], domain_for[target]
            if left != right:
                domain_weights[tuple(sorted((left, right)))] += weight
        domain_links = [
            {"source": source, "target": target, "weight": weight}
            for (source, target), weight in sorted(domain_weights.items())
            if weight > 0
        ]
        return {"domains": domains, "links": domain_links}

    if len(ids) < min_terms:
        return result_for([ids])

    adjacency: dict[str, dict[str, int]] = {term_id: {} for term_id in ids}
    for (source, target), weight in sorted(edge_weights.items()):
        adjacency[source][target] = weight
        adjacency[target][source] = weight

    # Louvain, first phase only: each Term moves to the neighbouring community with
    # the best modularity gain. Unlike plain label propagation this does not let a
    # single cross-link pull a whole group into its neighbour, because joining a big
    # community is penalised. Sorted order and smallest-id tie-breaks keep it
    # deterministic.
    labels = {term_id: term_id for term_id in ids}
    degree = {term_id: sum(adjacency[term_id].values()) for term_id in ids}
    total_weight = sum(degree.values()) or 1
    community_degree = dict(degree)
    for _ in range(20):
        moved = False
        for term_id in ids:
            current = labels[term_id]
            community_degree[current] -= degree[term_id]
            links_to: dict[str, int] = defaultdict(int)
            for neighbour, weight in adjacency[term_id].items():
                links_to[labels[neighbour]] += weight
            links_to.setdefault(current, 0)

            def gain(community: str) -> float:
                return (
                    links_to[community]
                    - community_degree.get(community, 0)
                    * degree[term_id]
                    / total_weight
                )

            best = max(
                sorted(links_to),
                key=lambda community: (gain(community), community == current),
            )
            community_degree[best] = community_degree.get(best, 0) + degree[term_id]
            if best != current:
                labels[term_id] = best
                moved = True
        if not moved:
            break

    groups_by_label: dict[str, list[str]] = defaultdict(list)
    for term_id in ids:
        groups_by_label[labels[term_id]].append(term_id)
    groups = [sorted(group) for _, group in sorted(groups_by_label.items())]

    # Gather all unconnected Terms under the honest, non-invented label Other.
    isolates = [term_id for term_id in ids if not adjacency[term_id]]
    if isolates:
        isolate_set = set(isolates)
        groups = [
            group for group in groups if not all(item in isolate_set for item in group)
        ]
        groups.append(isolates)

    def merge_smallest(
        groups_to_merge: list[list[str]], max_domains: int | None = None
    ) -> list[list[str]]:
        while len(groups_to_merge) > 1 and (
            (max_domains is not None and len(groups_to_merge) > max_domains)
            or (max_domains is None and min(map(len, groups_to_merge)) < 3)
        ):
            source_group = min(
                groups_to_merge, key=lambda group: (len(group), group[0])
            )
            candidates = [
                group for group in groups_to_merge if group is not source_group
            ]
            weights = {
                id(group): sum(
                    weight
                    for (left, right), weight in edge_weights.items()
                    if (left in source_group and right in group)
                    or (right in source_group and left in group)
                )
                for group in candidates
            }
            target_group = min(
                candidates,
                key=lambda group: (-weights[id(group)], group[0]),
            )
            source_is_isolated = not any(
                (left in source_group and right in target_group)
                or (right in source_group and left in target_group)
                for left, right in edge_weights
            )
            # A completely isolated tiny group is retained as its own Other domain.
            if source_is_isolated and max_domains is None:
                break
            target_group.extend(source_group)
            target_group.sort()
            groups_to_merge.remove(source_group)
        return groups_to_merge

    groups = merge_smallest(groups)
    groups = merge_smallest(groups, max_domains=12)
    result = result_for(groups)
    for domain in result["domains"]:
        if isolates and domain["term_ids"] == isolates:
            domain["name"] = "Other"
    return result
