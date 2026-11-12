# SPDX-License-Identifier: Apache-2.0

"""The shared-secret gate in front of the API and the ingestion service."""

from __future__ import annotations

import logging
from typing import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from auto_ontology.server import internal_auth
from auto_ontology.server.internal_auth import (
    BACKEND_PUBLIC_PATHS,
    INGESTION_PUBLIC_PATHS,
    InternalTokenError,
    InternalTokenMiddleware,
    check_internal_token_config,
    validate_internal_token,
)

# Obviously fake, but long and varied enough to pass the strength check.
TOKEN = "test-only-0123456789-abcdefghijklmnopqrstuvwxyz"
USER_HEADER = "x-auto-ontology-user-id"


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)


def _probe_app() -> FastAPI:
    """A tiny app behind the middleware that echoes the identity it can see."""
    app = FastAPI()
    app.add_middleware(InternalTokenMiddleware, public_paths=BACKEND_PUBLIC_PATHS)

    @app.get("/api/thing")
    def thing(request: Request) -> dict[str, str | None]:
        return {"user": request.headers.get(USER_HEADER)}

    @app.get("/api/health/live")
    def live(request: Request) -> dict[str, str | None]:
        return {"user": request.headers.get(USER_HEADER)}

    @app.get("/api/stream")
    def stream() -> StreamingResponse:
        def events() -> Iterator[str]:
            yield "data: one\n\n"
            yield "data: two\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def _backend_app() -> FastAPI:
    from auto_ontology.server.__main__ import create_app

    return create_app()


def _ingestion_app() -> FastAPI:
    from auto_ontology.ingestion_service.__main__ import app

    return app


# --- no token configured: unchanged behaviour ---------------------------------


def test_without_a_configured_token_everything_is_open() -> None:
    client = TestClient(_probe_app())

    response = client.get("/api/thing", headers={USER_HEADER: "user-1"})

    assert response.status_code == 200
    assert response.json() == {"user": "user-1"}


def test_empty_token_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_API_TOKEN", "")

    assert TestClient(_probe_app()).get("/api/thing").status_code == 200


def test_startup_warns_when_no_token_is_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=internal_auth.logger.name):
        check_internal_token_config("Lexis API")

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "unauthenticated" in record.getMessage()
    assert "INTERNAL_API_TOKEN" in record.getMessage()
    assert "openssl rand -hex 32" in record.getMessage()


# --- token configured -----------------------------------------------------------


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    return TOKEN


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-internal-token": "wrong-" + TOKEN},
        {"x-internal-token": TOKEN[:-1]},
        {"x-internal-token": ""},
        # The identity alone proves nothing.
        {USER_HEADER: "admin"},
    ],
)
def test_missing_or_wrong_token_is_401_without_detail(
    token: str, headers: dict[str, str]
) -> None:
    response = TestClient(_probe_app()).get("/api/thing", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_right_token_is_accepted_and_identity_passes_through(token: str) -> None:
    response = TestClient(_probe_app()).get(
        "/api/thing", headers={"x-internal-token": token, USER_HEADER: "user-1"}
    )

    assert response.status_code == 200
    assert response.json() == {"user": "user-1"}


def test_every_method_is_gated(token: str) -> None:
    client = TestClient(_probe_app())

    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/thing").status_code == 401


def test_streaming_bodies_survive_the_middleware(token: str) -> None:
    response = TestClient(_probe_app()).get(
        "/api/stream", headers={"x-internal-token": token}
    )

    assert response.status_code == 200
    assert response.text == "data: one\n\ndata: two\n\n"


def test_health_is_open_but_its_identity_header_is_stripped(token: str) -> None:
    client = TestClient(_probe_app())

    response = client.get("/api/health/live", headers={USER_HEADER: "forged"})

    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_only_the_listed_health_paths_are_open(token: str) -> None:
    client = TestClient(_probe_app())

    # Trailing slash, sibling and prefix of an open path are not open.
    for path in ("/api/health/live/", "/api/health/live/x", "/api/healthz", "/health"):
        assert client.get(path).status_code == 401, path


def test_comparison_is_constant_time(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object]] = []
    real = internal_auth.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(internal_auth.hmac, "compare_digest", spy)

    TestClient(_probe_app()).get("/api/thing", headers={"x-internal-token": token})

    assert calls == [(token.encode(), token.encode())]


def test_the_token_is_never_logged(
    token: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = TestClient(_probe_app())
    wrong = "wrong-" + TOKEN

    with caplog.at_level(logging.DEBUG):
        client.get("/api/thing", headers={"x-internal-token": wrong})
        client.get("/api/thing", headers={"x-internal-token": token})
        client.get("/api/thing")
        check_internal_token_config("Lexis API")

    assert TOKEN not in caplog.text
    assert wrong not in caplog.text
    rejected = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(rejected) == 2
    assert "GET /api/thing" in rejected[0].getMessage()


# --- startup validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "weak",
    [
        "abc1",
        "change-me",
        "a" * 64,  # long, but a single character
        "abababababababababababababababab",  # two distinct characters
        "0123456789" + "abcdefghijklmnopqrstuvwx yz",  # whitespace
    ],
)
def test_a_weak_token_fails_startup_without_echoing_it(
    weak: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERNAL_API_TOKEN", weak)

    with pytest.raises(InternalTokenError) as exc_info:
        check_internal_token_config("Lexis API")

    assert "INTERNAL_API_TOKEN" in str(exc_info.value)
    assert "openssl rand -hex 32" in str(exc_info.value)
    assert weak not in str(exc_info.value)


@pytest.mark.parametrize(
    "good",
    [TOKEN, "f" * 8 + "0123456789abcdef" * 2, "x7Q" * 4 + "A1b2C3d4E5f6G7h8I9j0"],
)
def test_a_strong_token_passes(good: str) -> None:
    validate_internal_token(good)


# --- the real apps ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_app", "protected", "public"),
    [
        (_backend_app, "/api/sql-attributes", "/api/health/live"),
        (_ingestion_app, "/semantic/status", "/health/live"),
    ],
)
def test_the_real_apps_refuse_requests_without_the_token(
    make_app: object, protected: str, public: str, token: str
) -> None:
    client = TestClient(make_app())  # type: ignore[operator]

    assert client.get(protected).status_code == 401
    assert (
        client.get(protected, headers={"x-internal-token": "nope" * 10}).status_code
        == 401
    )
    assert client.post("/api/chat/completions").status_code == 401
    assert client.get(public).status_code == 200
    # With the token the request reaches routing (404), not the gate.
    assert (
        client.get("/no/such/route", headers={"x-internal-token": token}).status_code
        == 404
    )


def test_the_real_apps_allow_only_their_probe_paths() -> None:
    assert BACKEND_PUBLIC_PATHS == {"/api/health", "/api/health/live"}
    assert INGESTION_PUBLIC_PATHS == {"/health", "/health/live"}


def test_cors_preflight_is_answered_before_the_gate(token: str) -> None:
    response = TestClient(_backend_app()).options(
        "/api/chat/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200


# --- the API's own calls to the ingestion service ---------------------------------


def _record_ingestion_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Run every proxy call against a stub that records the headers it was sent."""
    from auto_ontology.server.ingestion import proxy

    calls: list[dict[str, object]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, bool]:
            return {"running": False}

    def fake(url: str, **kwargs: object) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(proxy.httpx, "post", fake)
    monkeypatch.setattr(proxy.httpx, "get", fake)
    proxy.trigger_ingest({"database_name": "d"})
    proxy.trigger_semantic_compile()
    proxy.is_semantic_compilation_running()
    proxy.trigger_semantic_reset("d")
    proxy.trigger_reset("d")
    return calls


def test_every_ingestion_call_carries_the_token(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_ingestion_calls(monkeypatch)

    assert len(calls) == 5
    assert all(c["headers"] == {"x-internal-token": token} for c in calls)


def test_ingestion_calls_send_no_header_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_ingestion_calls(monkeypatch)

    assert len(calls) == 5
    assert all(c["headers"] == {} for c in calls)
