# SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
# SPDX-License-Identifier: Apache-2.0

"""Create, delete and relink a Term through its routes.

The DAL and the vector index are replaced with fakes, so these tests check the
routes' contract (status codes, who is recorded, what is embedded and
unembedded, in which order) and not SQL; ``dal/tests/test_terms.py`` runs the
SQL against Postgres.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from auto_ontology.dal import terms as terms_dal
from auto_ontology.server import owners
from auto_ontology.server.terms import router, service
from auto_ontology.server.terms.router import TermCreate, TermUpdate

USER = "user-7"


def _request(user: str | None = USER) -> Request:
    headers = [(b"x-auto-ontology-user-id", user.encode())] if user else []
    return Request(
        {"type": "http", "method": "POST", "path": "/api/terms", "headers": headers}
    )


class FakeVdb:
    """Records ``delete_by_id`` calls and the order they happen in."""

    def __init__(self, events: list[str], rows: dict[str, int] | None = None) -> None:
        self.events = events
        self.rows = rows or {}

    def delete_by_id(self, node_id: str) -> int:
        self.events.append(f"vdb.delete:{node_id}")
        return self.rows.get(node_id, 0)


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def fake_dal(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> dict[str, Any]:
    """A DAL with one database ``shop`` holding tables ``orders`` and ``items``."""
    state: dict[str, Any] = {"created": []}
    tables = [
        {"id": "t-items", "name": "items", "columns": ["id", "sku_code"]},
        {"id": "t-orders", "name": "orders", "columns": ["id", "customer_id"]},
    ]

    def resolve_database(database: str) -> dict[str, Any] | None:
        return {"id": "db-1", "name": "shop"} if database in ("shop", "db-1") else None

    def database_for_tables(ids: list[str]) -> dict[str, Any]:
        known = {"t-items", "t-orders"}
        unknown = [i for i in ids if i not in known and i != "t-other"]
        if unknown:
            raise terms_dal.UnknownTableIds(unknown)
        if "t-other" in ids:
            raise terms_dal.TermSpansDatabases(["shop", "hr"])
        return {"id": "db-1", "name": "shop"}

    def create_term(**kwargs: Any) -> dict[str, Any]:
        events.append("dal.create")
        if kwargs["name"] == "Taken":
            raise terms_dal.TermNameTaken("Taken")
        state["created"].append(kwargs)
        return {
            "id": "term-1",
            "name": kwargs["name"],
            "description": kwargs["description"],
            "synonyms": kwargs["synonyms"] or [],
            "name_certified": False,
            "description_certified": False,
            "owner_id": kwargs["owner_id"],
            "created_by": kwargs["created_by"],
            "table_ids": kwargs["table_ids"],
        }

    monkeypatch.setattr(terms_dal, "resolve_database", resolve_database)
    monkeypatch.setattr(terms_dal, "database_for_tables", database_for_tables)
    monkeypatch.setattr(terms_dal, "fetch_database_link_candidates", lambda _id: tables)
    monkeypatch.setattr(terms_dal, "create_term", create_term)
    monkeypatch.setattr(terms_dal, "get_term_certification", lambda _id: "uncertified")
    monkeypatch.setattr(
        service,
        "refresh_term_embeddings",
        lambda term_id, *, refresh_dependent_attrs: (
            events.append(f"embed:{term_id}") or True
        ),
    )
    monkeypatch.setattr(owners.postgres_dal, "get_user_role", lambda _u: "admin")
    return state


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_create_records_the_caller_defaults_to_uncertified_and_embeds(
    fake_dal: dict[str, Any], events: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=router.logger.name)

    out = router.create_term(
        _request(),
        TermCreate(
            name="  Net revenue ",
            description="Order total minus refunds",
            synonyms=["net sales", " ", "net sales"],
            table_ids=["t-orders"],
        ),
    )["data"]

    saved = fake_dal["created"][0]
    assert saved["name"] == "Net revenue"
    assert saved["created_by"] == USER
    assert saved["table_ids"] == ["t-orders"]
    assert saved["synonyms"] == ["net sales"]  # blank dropped, duplicate folded
    assert out["linked_by"] == "explicit"
    assert out["embedded"] is True
    assert out["name_certified"] is False and out["description_certified"] is False
    # Embedded after the row exists, once, by the new id.
    assert events == ["dal.create", "embed:term-1"]
    audit = [
        r.getMessage() for r in caplog.records if "audit: term.create" in r.getMessage()
    ]
    assert len(audit) == 1 and USER in audit[0] and "Net revenue" in audit[0]


def test_create_links_by_the_tables_and_columns_the_text_names(
    fake_dal: dict[str, Any],
) -> None:
    out = router.create_term(
        _request(),
        TermCreate(
            name="Repeat buyer",
            description="A customer_id that appears on two or more orders",
            database="shop",
        ),
    )["data"]

    # `customer_id` is a column of orders and `orders` is its name; `items` is
    # not mentioned and `id` is too short to count.
    assert fake_dal["created"][0]["table_ids"] == ["t-orders"]
    assert out["linked_by"] == "matched"
    assert out["database_name"] == "shop"


def test_create_without_a_match_falls_back_to_one_table_and_says_so(
    fake_dal: dict[str, Any],
) -> None:
    out = router.create_term(
        _request(), TermCreate(name="Churn", description="Left us", database="db-1")
    )["data"]

    assert fake_dal["created"][0]["table_ids"] == ["t-items"]  # first by name
    assert out["linked_by"] == "fallback"


def test_a_duplicate_name_is_a_409_and_nothing_is_embedded(
    fake_dal: dict[str, Any], events: list[str]
) -> None:
    with pytest.raises(HTTPException) as refused:
        router.create_term(_request(), TermCreate(name="Taken", table_ids=["t-orders"]))

    assert refused.value.status_code == 409
    assert "Taken" in refused.value.detail
    assert events == ["dal.create"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (TermCreate(name="x", database="nope"), 404),
        (TermCreate(name="x", table_ids=["t-missing"]), 422),
        (TermCreate(name="x", table_ids=["t-orders", "t-other"]), 422),
        (TermCreate(name="x", table_ids=["t-orders"], database="nope"), 404),
        (TermCreate(name="   ", database="shop"), 422),
    ],
)
def test_bad_scope_is_refused_before_any_write(
    fake_dal: dict[str, Any], events: list[str], body: TermCreate, status: int
) -> None:
    with pytest.raises(HTTPException) as refused:
        router.create_term(_request(), body)

    assert refused.value.status_code == status
    assert events == []


def test_a_create_needs_a_database_or_tables() -> None:
    with pytest.raises(ValidationError):
        TermCreate(name="x")


def test_a_failing_embed_keeps_the_term_and_reports_it(
    fake_dal: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(term_id: str, *, refresh_dependent_attrs: bool) -> bool:
        raise RuntimeError("embedding endpoint down")

    monkeypatch.setattr(service, "refresh_term_embeddings", boom)

    out = router.create_term(
        _request(), TermCreate(name="Net", table_ids=["t-orders"])
    )["data"]

    assert out["id"] == "term-1"
    assert out["embedded"] is False


def test_a_viewer_cannot_create(
    fake_dal: dict[str, Any],
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(owners.postgres_dal, "get_user_role", lambda _u: "viewer")

    with pytest.raises(HTTPException) as refused:
        router.create_term(_request(), TermCreate(name="x", table_ids=["t-orders"]))

    assert refused.value.status_code == 403
    assert events == []


def test_a_create_with_no_identity_header_is_trusted_like_other_writes(
    fake_dal: dict[str, Any],
) -> None:
    router.create_term(_request(None), TermCreate(name="x", table_ids=["t-orders"]))

    assert fake_dal["created"][0]["created_by"] is None


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


@pytest.fixture
def delete_world(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> dict[str, Any]:
    state: dict[str, Any] = {"sql_attributes": []}

    def vector_ids(term_id: str) -> dict[str, Any] | None:
        if term_id != "term-1" or state.get("deleted"):
            return None
        return {
            "id": "term-1",
            "name": "Net revenue",
            "attribute_ids": ["attr-1", "attr-2"],
            "sql_attribute_names": state["sql_attributes"],
            "database_names": ["shop"],
        }

    def delete(term_id: str) -> bool:
        events.append("dal.delete")
        state["deleted"] = True
        return True

    monkeypatch.setattr(terms_dal, "fetch_term_vector_ids", vector_ids)
    monkeypatch.setattr(terms_dal, "delete_term", delete)
    monkeypatch.setattr(
        service,
        "get_semantic_vdb",
        lambda **_: FakeVdb(events, {"term-1": 1, "attr-1": 1, "attr-2": 1}),
    )
    monkeypatch.setattr(router, "edit_subject", lambda _id: None)
    return state


def test_delete_removes_vector_rows_before_the_database_rows(
    delete_world: dict[str, Any],
    events: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=router.logger.name)

    out = router.delete_term(_request(), "term-1")["data"]

    # The Term's own row and each ColumnAttribute's, then the database.
    assert events == [
        "vdb.delete:term-1",
        "vdb.delete:attr-1",
        "vdb.delete:attr-2",
        "dal.delete",
    ]
    assert out == {
        "id": "term-1",
        "name": "Net revenue",
        "column_attributes_deleted": 2,
        "vector_rows_deleted": 3,
    }
    audit = [
        r.getMessage() for r in caplog.records if "audit: term.delete" in r.getMessage()
    ]
    assert len(audit) == 1 and USER in audit[0] and "term-1" in audit[0]


def test_deleting_twice_is_a_404_the_second_time(delete_world: dict[str, Any]) -> None:
    router.delete_term(_request(), "term-1")

    with pytest.raises(HTTPException) as gone:
        router.delete_term(_request(), "term-1")

    assert gone.value.status_code == 404


def test_deleting_an_unknown_term_touches_nothing(
    delete_world: dict[str, Any], events: list[str]
) -> None:
    with pytest.raises(HTTPException) as missing:
        router.delete_term(_request(), "nope")

    assert missing.value.status_code == 404
    assert events == []


def test_a_term_with_sql_attributes_is_a_409_and_nothing_is_removed(
    delete_world: dict[str, Any], events: list[str]
) -> None:
    delete_world["sql_attributes"] = ["VIP customer"]

    with pytest.raises(HTTPException) as blocked:
        router.delete_term(_request(), "term-1")

    assert blocked.value.status_code == 409
    assert "VIP customer" in blocked.value.detail
    assert events == []


def test_a_failing_index_delete_leaves_the_database_untouched(
    delete_world: dict[str, Any],
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(FakeVdb):
        def delete_by_id(self, node_id: str) -> int:
            raise RuntimeError("vector store down")

    monkeypatch.setattr(service, "get_semantic_vdb", lambda **_: Broken(events))

    with pytest.raises(RuntimeError):
        router.delete_term(_request(), "term-1")

    assert "dal.delete" not in events


def test_delete_is_checked_against_the_terms_owner(
    delete_world: dict[str, Any],
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_ontology.dal.owners import EditSubject

    seen: list[EditSubject] = []

    def refuse(_request: Request, subject: EditSubject) -> None:
        seen.append(subject)
        raise HTTPException(status_code=403, detail="no")

    monkeypatch.setattr(
        router, "edit_subject", lambda _id: EditSubject("catalog:edit", None)
    )
    monkeypatch.setattr(router, "enforce_edit", refuse)

    with pytest.raises(HTTPException) as refused:
        router.delete_term(_request(), "term-1")

    assert refused.value.status_code == 403
    assert seen and seen[0].permission == "catalog:edit"
    assert events == []


# --------------------------------------------------------------------------
# Patch: links and conflicts
# --------------------------------------------------------------------------


@pytest.fixture
def patch_world(
    fake_dal: dict[str, Any], monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    def update_term(term_id: str, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs.get("name") == "Taken":
            raise terms_dal.TermNameTaken("Taken")
        if term_id != "term-1":
            return None
        return {
            "id": "term-1",
            "name": kwargs.get("name") or "Net revenue",
            "old_name": "Net revenue",
            "description": kwargs.get("description"),
            "name_certified": False,
            "description_certified": False,
            "owner_id": None,
        }

    monkeypatch.setattr(terms_dal, "update_term", update_term)
    monkeypatch.setattr(
        terms_dal,
        "replace_term_tables",
        lambda term_id, ids: events.append(f"relink:{','.join(ids)}"),
    )
    monkeypatch.setattr(router, "edit_subject", lambda _id: None)
    monkeypatch.setattr(
        router.term_service,
        "refresh_term_embeddings",
        lambda term_id, *, refresh_dependent_attrs: (
            events.append(f"embed:{term_id}") or True
        ),
    )


def test_patch_can_replace_the_tables_and_re_embeds(
    patch_world: None, events: list[str]
) -> None:
    router.update_term(
        _request(), "term-1", TermUpdate(table_ids=["t-items", "t-orders"])
    )

    assert events == ["relink:t-items,t-orders", "embed:term-1"]


def test_patch_with_bad_tables_changes_nothing(
    patch_world: None, events: list[str]
) -> None:
    with pytest.raises(HTTPException) as refused:
        router.update_term(_request(), "term-1", TermUpdate(table_ids=["t-missing"]))

    assert refused.value.status_code == 422
    assert events == []


def test_patch_cannot_empty_a_terms_tables() -> None:
    with pytest.raises(ValidationError):
        TermUpdate(table_ids=[])


def test_patch_rename_to_a_taken_name_is_a_409(patch_world: None) -> None:
    with pytest.raises(HTTPException) as refused:
        router.update_term(_request(), "term-1", TermUpdate(name="Taken"))

    assert refused.value.status_code == 409


def test_patch_description_change_re_embeds_a_certification_flag_does_not(
    patch_world: None, events: list[str]
) -> None:
    router.update_term(_request(), "term-1", TermUpdate(name_certified=True))
    assert events == []

    router.update_term(_request(), "term-1", TermUpdate(description="new"))
    assert events == ["embed:term-1"]


# --------------------------------------------------------------------------
# Pure pieces
# --------------------------------------------------------------------------


def test_link_matching_uses_whole_words_of_four_or_more_characters() -> None:
    tables = [
        {"id": "a", "name": "orders", "columns": ["id", "status"]},
        {"id": "b", "name": "payments", "columns": ["amount"]},
    ]

    assert service.link_tables_for_text("Total AMOUNT paid", tables) == (
        ["b"],
        "matched",
    )
    # "id" is too short, and "disorders" does not contain the word "orders".
    assert service.link_tables_for_text("an id of disorders", tables) == (
        ["a"],
        "fallback",
    )
    assert service.link_tables_for_text("anything", []) == ([], "fallback")


def test_the_embedding_row_is_replaced_not_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refresh_term_embeddings deletes the Term's row by id, then writes one."""
    calls: list[str] = []

    class Embedder:
        vdb = FakeVdb(calls)

        def embed_term(self, term: dict[str, Any], attrs: list[Any]) -> int:
            calls.append(f"embed:{term['name']}:{len(attrs)}")
            return 1

    monkeypatch.setattr(
        terms_dal,
        "fetch_term_and_column_attributes_for_embedding",
        lambda _id: (
            {"id": "term-1", "name": "Net revenue", "database_name": "shop"},
            [{"id": "attr-1"}],
        ),
    )
    monkeypatch.setattr(service, "build_semantic_embedder", lambda *a, **k: Embedder())

    assert service.refresh_term_embeddings("term-1", refresh_dependent_attrs=False)
    assert calls == ["vdb.delete:term-1", "embed:Net revenue:0"]
