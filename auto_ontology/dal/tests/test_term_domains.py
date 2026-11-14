# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


from auto_ontology.dal.term_domains import cluster_terms


def _cliques(size: int = 4) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    ids = [f"a{i}" for i in range(size)] + [f"z{i}" for i in range(size)]
    nodes = [
        {"id": term_id, "name": f"Term {term_id}", "relationship_count": 0}
        for term_id in ids
    ]
    links = [
        {"source": left, "target": right}
        for group in (ids[:size], ids[size:])
        for index, left in enumerate(group)
        for right in group[index + 1 :]
    ]
    links.append({"source": f"a{size - 1}", "target": "z0"})
    return nodes, links


def test_two_cliques_form_domains_with_one_weighted_link() -> None:
    nodes, links = _cliques()
    result = cluster_terms(nodes, links, min_terms=1)
    assert {frozenset(domain["term_ids"]) for domain in result["domains"]} == {
        frozenset(f"a{i}" for i in range(4)),
        frozenset(f"z{i}" for i in range(4)),
    }
    assert len(result["links"]) == 1
    assert result["links"][0]["weight"] == 1


def test_small_input_is_one_domain() -> None:
    result = cluster_terms([{"id": "x", "name": "X"}], [], min_terms=30)
    assert len(result["domains"]) == 1
    assert result["domains"][0]["term_ids"] == ["x"]


def test_isolated_terms_are_grouped_as_other() -> None:
    nodes, links = _cliques()
    nodes.extend(
        {"id": key, "name": key, "relationship_count": 0} for key in ("solo1", "solo2")
    )
    result = cluster_terms(nodes, links, min_terms=1)
    other = next(
        domain
        for domain in result["domains"]
        if set(domain["term_ids"]) == {"solo1", "solo2"}
    )
    assert other["name"] == "Other"


def test_input_order_does_not_change_output() -> None:
    nodes, links = _cliques()
    first = cluster_terms(nodes, links, min_terms=1)
    second = cluster_terms(list(reversed(nodes)), list(reversed(links)), min_terms=1)
    assert first == second


def test_domain_count_is_capped_at_twelve() -> None:
    nodes: list[dict[str, object]] = []
    links: list[dict[str, str]] = []
    for group in range(13):
        members = [f"g{group:02d}-{index}" for index in range(3)]
        nodes.extend(
            {"id": member, "name": member, "relationship_count": 2}
            for member in members
        )
        links.extend({"source": members[0], "target": member} for member in members[1:])
    assert len(cluster_terms(nodes, links, min_terms=1)["domains"]) == 12


def test_domain_name_uses_highest_relationship_count_then_alphabetical() -> None:
    nodes = [
        {"id": "a", "name": "Zulu", "relationship_count": 3},
        {"id": "b", "name": "Alpha", "relationship_count": 3},
        {"id": "c", "name": "Centre", "relationship_count": 1},
    ]
    result = cluster_terms(
        nodes,
        [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        min_terms=1,
    )
    assert result["domains"][0]["name"] == "Alpha"
