# SPDX-License-Identifier: Apache-2.0

"""Business-domain routes with service and persistence fakes."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import HTTPException, Request

from auto_ontology.dal import domains, terms
from auto_ontology.server.exploration import router, service
from auto_ontology.server.exploration.router import (
    SemanticDomainRename,
    SemanticDomainTerms,
)


def _request(user: str | None = "user-7") -> Request:
    headers = [(b"x-auto-ontology-user-id", user.encode())] if user else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/exploration/semantic-domains",
            "headers": headers,
        }
    )


@pytest.fixture
def writable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "enforce_permission", lambda *_args: None)


def test_generate_groups_and_persists_with_fake_llm(
    monkeypatch: pytest.MonkeyPatch,
    writable: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    persisted: list[dict[str, Any]] = []
    fake_llm = object()
    monkeypatch.setattr(
        terms, "resolve_database", lambda _id: {"id": "db-1", "name": "shop"}
    )
    monkeypatch.setattr(
        service,
        "fetch_semantic_exploration_graph",
        lambda **_kwargs: {"nodes": [{"id": "t-1", "name": "Revenue"}], "links": []},
    )
    monkeypatch.setattr(
        service.connections, "get_ontology_language", lambda _name: "en"
    )
    monkeypatch.setattr(
        "auto_ontology.utils.llm_invoke.get_llm_client", lambda **_kwargs: fake_llm
    )

    def grouping(
        terms_: list[dict[str, Any]], language: str, llm: Any
    ) -> list[dict[str, Any]]:
        assert terms_ and language == "en" and llm is fake_llm
        return [{"name": "Finance", "description": None, "term_ids": ["t-1"]}]

    def store(database_id: str, groups: list[dict[str, Any]]) -> None:
        assert database_id == "db-1"
        persisted.extend(groups)

    monkeypatch.setattr(service, "group_terms", grouping)
    monkeypatch.setattr(domains, "replace_auto_domains", store)
    monkeypatch.setattr(
        service,
        "fetch_semantic_domains",
        lambda _id: {"generated": True, "domains": [], "links": []},
    )
    caplog.set_level(logging.INFO, logger=router.logger.name)

    out = router.generate_semantic_domains(_request(), "db-1")

    assert persisted == [{"name": "Finance", "description": None, "term_ids": ["t-1"]}]
    assert out["data"]["generated"] is True
    assert any(
        "audit: semantic_domain.generate" in record.getMessage()
        for record in caplog.records
    )


def test_read_unknown_database_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "fetch_semantic_domains",
        lambda _id: (_ for _ in ()).throw(service.UnknownDatabase("missing")),
    )
    with pytest.raises(HTTPException) as error:
        router.get_semantic_domains("missing")
    assert error.value.status_code == 404


def test_write_permission_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_args: Any) -> None:
        raise HTTPException(status_code=403)

    monkeypatch.setattr(router, "enforce_permission", deny)
    with pytest.raises(HTTPException) as error:
        router.generate_semantic_domains(_request(), "db-1")
    assert error.value.status_code == 403


def test_rename_unknown_duplicate_and_success(
    monkeypatch: pytest.MonkeyPatch, writable: None
) -> None:
    def unknown(*_args: Any) -> dict[str, Any]:
        raise service.UnknownDomain("missing")

    monkeypatch.setattr(service, "rename_semantic_domain", unknown)
    with pytest.raises(HTTPException) as error:
        router.rename_semantic_domain(
            _request(), "missing", SemanticDomainRename(name="X")
        )
    assert error.value.status_code == 404

    def duplicate(*_args: Any) -> dict[str, Any]:
        raise service.DuplicateDomainName("Finance")

    monkeypatch.setattr(service, "rename_semantic_domain", duplicate)
    with pytest.raises(HTTPException) as error:
        router.rename_semantic_domain(
            _request(), "d-1", SemanticDomainRename(name="Finance")
        )
    assert error.value.status_code == 409

    monkeypatch.setattr(
        service,
        "rename_semantic_domain",
        lambda *_args: {"generated": True, "domains": [], "links": []},
    )
    assert router.rename_semantic_domain(
        _request(), "d-1", SemanticDomainRename(name="Sales")
    )["data"]["generated"]


def test_move_unknown_domain_and_term_ids(
    monkeypatch: pytest.MonkeyPatch, writable: None
) -> None:
    monkeypatch.setattr(
        service,
        "move_semantic_domain_terms",
        lambda *_args: (_ for _ in ()).throw(service.UnknownDomain("missing")),
    )
    with pytest.raises(HTTPException) as error:
        router.move_semantic_domain_terms(
            _request(), "missing", SemanticDomainTerms(term_ids=[])
        )
    assert error.value.status_code == 404

    monkeypatch.setattr(
        service,
        "move_semantic_domain_terms",
        lambda *_args: (_ for _ in ()).throw(ValueError("Unknown Term ids")),
    )
    with pytest.raises(HTTPException) as error:
        router.move_semantic_domain_terms(
            _request(), "d-1", SemanticDomainTerms(term_ids=["unknown"])
        )
    assert error.value.status_code == 422
