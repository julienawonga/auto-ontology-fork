# SPDX-License-Identifier: Apache-2.0

"""Shared secret between the web app and the Python services.

The API (``:3001``) and the ingestion service (``:3002``) have no accounts of
their own: they trust whatever the Next.js web app forwards, including the
``x-auto-ontology-user-id`` header. Network placement (loopback binding,
ClusterIP) keeps strangers out, but anyone who can reach either port bypasses
every permission the web app enforces. When ``INTERNAL_API_TOKEN`` is set, both
services refuse any request that does not present it in ``x-internal-token``.

Unset, behaviour is unchanged and a WARNING at startup says so, so existing
deployments keep working until the operator opts in.

This module depends on the standard library only: the ingestion client
(``server/ingestion/proxy.py``), which is deliberately lightweight, imports it
too.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

INTERNAL_TOKEN_ENV = "INTERNAL_API_TOKEN"
INTERNAL_TOKEN_HEADER = "x-internal-token"
# Mirrors ``identity.INTERNAL_USER_HEADER``; not imported so this module stays
# free of FastAPI.
_USER_HEADER = b"x-auto-ontology-user-id"

MIN_TOKEN_LENGTH = 32
MIN_DISTINCT_CHARS = 10

# What Docker and Kubernetes probe. The kubelet and the Docker healthcheck
# cannot present a secret, and these four answers carry no catalog data.
BACKEND_PUBLIC_PATHS = frozenset({"/api/health", "/api/health/live"})
INGESTION_PUBLIC_PATHS = frozenset({"/health", "/health/live"})

_GENERATE_HINT = "generate one with `openssl rand -hex 32`"


class InternalTokenError(RuntimeError):
    """``INTERNAL_API_TOKEN`` is set but unusable."""


def get_internal_token() -> str | None:
    """The configured token, or ``None`` when authentication is off.

    An empty value counts as unset: ``INTERNAL_API_TOKEN=`` in ``.env`` and a
    compose ``${INTERNAL_API_TOKEN:-}`` both produce one.
    """
    return os.environ.get(INTERNAL_TOKEN_ENV) or None


def validate_internal_token(token: str) -> None:
    """Refuse a token too weak to be a shared secret.

    Checked at startup rather than per request: a guessable token is worse
    than none because it looks like protection.
    """
    problems: list[str] = []
    if len(token) < MIN_TOKEN_LENGTH:
        problems.append(f"it is shorter than {MIN_TOKEN_LENGTH} characters")
    if len(set(token)) < MIN_DISTINCT_CHARS:
        problems.append(f"it has fewer than {MIN_DISTINCT_CHARS} distinct characters")
    if any(c.isspace() for c in token):
        problems.append("it contains whitespace")
    if problems:
        # Never echo the value: this goes to the logs.
        raise InternalTokenError(
            f"{INTERNAL_TOKEN_ENV} is set but too weak ({'; '.join(problems)}); "
            f"{_GENERATE_HINT}."
        )


def check_internal_token_config(service: str) -> None:
    """Startup gate: validate the token, or warn loudly that there is none."""
    token = get_internal_token()
    if token is None:
        logger.warning(
            "%s is unauthenticated: %s is not set, so every request is accepted "
            "and the forwarded user identity is trusted. Anyone who can reach "
            "this port bypasses the web app's permissions. Set %s to the same "
            "random value on the web app, the API and the ingestion service "
            "(%s). See SECURITY.md.",
            service,
            INTERNAL_TOKEN_ENV,
            INTERNAL_TOKEN_ENV,
            _GENERATE_HINT,
        )
        return
    validate_internal_token(token)
    logger.info("%s requires the internal API token on every request", service)


def token_is_valid(presented: bytes | None, expected: str) -> bool:
    """Constant-time comparison; a missing token never matches."""
    if not presented:
        return False
    return hmac.compare_digest(presented, expected.encode("utf-8"))


class InternalTokenMiddleware:
    """Reject requests without a valid ``x-internal-token``.

    Pure ASGI rather than ``BaseHTTPMiddleware``: the chat endpoints stream
    server-sent events, and the latter buffers and re-wraps streaming bodies.

    The token is read from the environment on each request so one process-wide
    setting governs every app instance; with it unset the middleware is a
    pass-through. Allow-listed paths are served without a token but with the
    identity header stripped, so a forged identity is never honoured on a
    request that has not proved who it is.
    """

    def __init__(self, app: ASGIApp, *, public_paths: Iterable[str]) -> None:
        self.app = app
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        expected = get_internal_token()
        if expected is None or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        presented = next(
            (
                value
                for name, value in headers
                if name.lower() == INTERNAL_TOKEN_HEADER.encode("ascii")
            ),
            None,
        )
        if token_is_valid(presented, expected):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("path") in self.public_paths:
            scope = {
                **scope,
                "headers": [(n, v) for n, v in headers if n.lower() != _USER_HEADER],
            }
            await self.app(scope, receive, send)
            return

        # Method and path only: the header values are secrets or forgeries.
        logger.warning(
            "Rejected request without a valid internal token: %s %s",
            scope.get("method", scope["type"]),
            scope.get("path"),
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        # 401 with no detail: do not say whether the token was missing or wrong.
        body = b'{"detail":"Unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
