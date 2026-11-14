# SPDX-License-Identifier: Apache-2.0
"""Pure tests for composing stored domains with the semantic graph."""

from auto_ontology.dal import exploration


def test_fetch_semantic_domains_aggregates_domains_and_links(monkeypatch):
    monkeypatch.setattr(
        exploration,
        "fetch_semantic_exploration_graph",
        lambda **kwargs: {
            "nodes": [
                {
                    "id": "a",
                    "name": "Alpha",
                    "relationship_count": 2,
                    "column_attributes_count": 1,
                    "sql_attributes_count": 2,
                },
                {
                    "id": "b",
                    "name": "Beta",
                    "relationship_count": 3,
                    "column_attributes_count": 0,
                    "sql_attributes_count": 1,
                },
                {
                    "id": "c",
                    "name": "Gamma",
                    "relationship_count": 1,
                    "column_attributes_count": 2,
                    "sql_attributes_count": 0,
                },
            ],
            "links": [
                {"source": "a", "target": "b", "relationship_types": ["represents"]},
                {"source": "b", "target": "c", "relationship_types": ["has_attribute"]},
                {"source": "a", "target": "a", "relationship_types": ["represents"]},
            ],
        },
    )
    monkeypatch.setattr(
        exploration,
        "fetch_domains",
        lambda database_id: [
            {
                "id": "d1",
                "name": "Core",
                "description": "Core terms",
                "origin": "human",
                "term_ids": ["a", "b"],
            }
        ],
    )
    monkeypatch.setattr(
        exploration,
        "fetch_term_table_pairs",
        lambda **kwargs: [
            {"term_id": "a", "table_id": "t1"},
            {"term_id": "b", "table_id": "t1"},
            {"term_id": "b", "table_id": "t2"},
            {"term_id": "c", "table_id": "t2"},
        ],
    )
    monkeypatch.setattr(
        exploration,
        "fetch_all_terms",
        lambda **kwargs: [
            {"id": "a", "certification": "certified"},
            {"id": "b", "certification": "partial"},
            {"id": "c", "certification": "partial"},
        ],
    )

    result = exploration.fetch_semantic_domains("db")

    assert result["generated"] is True
    assert result["domains"][0] == {
        "id": "d1",
        "name": "Core",
        "description": "Core terms",
        "origin": "human",
        "term_ids": ["a", "b"],
        "term_count": 2,
        "table_count": 2,
        "certified_term_count": 1,
        "column_attribute_count": 1,
        "sql_attribute_count": 3,
        "internal_link_count": 1,
        "top_terms": [{"id": "b", "name": "Beta"}, {"id": "a", "name": "Alpha"}],
    }
    assert result["domains"][1]["id"] == "unassigned"
    assert result["domains"][1]["term_ids"] == ["c"]
    assert result["links"] == [
        {
            "source": "d1",
            "target": "unassigned",
            "weight": 1,
            "shared_table_count": 1,
            "relationship_types": ["has_attribute"],
        }
    ]


def test_fetch_semantic_domains_without_stored_domains(monkeypatch):
    monkeypatch.setattr(
        exploration,
        "fetch_semantic_exploration_graph",
        lambda **kwargs: {"nodes": [], "links": []},
    )
    monkeypatch.setattr(exploration, "fetch_domains", lambda database_id: [])
    monkeypatch.setattr(exploration, "fetch_term_table_pairs", lambda **kwargs: [])
    monkeypatch.setattr(exploration, "fetch_all_terms", lambda **kwargs: [])

    assert exploration.fetch_semantic_domains("db") == {
        "generated": False,
        "domains": [],
        "links": [],
    }
