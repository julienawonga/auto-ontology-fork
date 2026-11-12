# SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
# SPDX-License-Identifier: Apache-2.0

"""Outbound HTTP discipline shared by every LLM, embedding and rerank call.

Two protections live at the HTTP transport, so every path that reaches the
model gateway gets them without each call site re-implementing them:

* **A process-wide cap on in-flight requests** (``LLM_MAX_CONCURRENT_REQUESTS``).
  N concurrent questions in one process, each fanning out into embedding
  batches and LLM calls, can no longer open N x fan-out requests at once. The
  slot is held until the response body has been read (or the stream closed).
* **Backoff on HTTP 429 and 503**: exponential with full jitter, ``Retry-After``
  honoured when the response carries it, bounded by a total wait budget
  (``LLM_RATE_LIMIT_BUDGET_S``). The slot is released while waiting.

Counting real HTTP attempts: the transport is the only layer that retries a
429/503. When its budget runs out it returns the last response marked
``x-should-retry: false`` (which the OpenAI SDK obeys, so the SDK's own
``max_retries`` cannot multiply it) and ``x-lexis-backoff-exhausted: 1`` (which
the application-level retry loops check through :func:`backoff_exhausted`, so
they neither retry nor fall back to another model through the same gateway).
Other transient failures (connection errors, 408/409/500/502/504) keep their
existing handling.

Only the synchronous client is wrapped (``GatewayClient.send``, which keeps
httpx's environment-aware transports): the code base makes no async calls to
the gateway. The cap is per process; see ``docs/configuration.md`` for how it
combines with the per-conversation worker subprocesses.
"""

from __future__ import annotations

import email.utils
import logging
import os
import random
import threading
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_REQUESTS = 8
DEFAULT_RATE_LIMIT_BUDGET_S = 60.0
DEFAULT_RATE_LIMIT_BASE_DELAY_S = 2.0
DEFAULT_RATE_LIMIT_MAX_DELAY_S = 30.0
DEFAULT_EMBED_HTTP_CONCURRENCY = 4
DEFAULT_SLOT_TIMEOUT_S = 300.0
# Log a WARNING when a request waits longer than this for a slot (saturation).
SLOT_WAIT_WARN_S = 5.0

# Hard ceiling on retries of one request, whatever the budget says, so a tiny
# base delay cannot turn a long budget into a storm.
MAX_RATE_LIMIT_RETRIES = 8

RATE_LIMIT_STATUSES = frozenset({429, 503})
_EXHAUSTED_HEADER = "x-lexis-backoff-exhausted"


def _env_number(name: str, default: float, *, minimum: float, integer: bool) -> float:
    """The environment value of *name*, or *default* with a warning when invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
        if value != value or value < minimum:  # NaN or below the minimum
            raise ValueError(raw)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r: expected %s >= %s; using %s.",
            name,
            raw,
            "an integer" if integer else "a number",
            minimum,
            default,
        )
        return default
    return value


def max_concurrent_requests() -> int:
    return int(
        _env_number(
            "LLM_MAX_CONCURRENT_REQUESTS",
            DEFAULT_MAX_CONCURRENT_REQUESTS,
            minimum=1,
            integer=True,
        )
    )


def rate_limit_budget_s() -> float:
    return _env_number(
        "LLM_RATE_LIMIT_BUDGET_S",
        DEFAULT_RATE_LIMIT_BUDGET_S,
        minimum=0,
        integer=False,
    )


def rate_limit_base_delay_s() -> float:
    return _env_number(
        "LLM_RATE_LIMIT_BASE_DELAY_S",
        DEFAULT_RATE_LIMIT_BASE_DELAY_S,
        minimum=0.01,
        integer=False,
    )


def rate_limit_max_delay_s() -> float:
    return _env_number(
        "LLM_RATE_LIMIT_MAX_DELAY_S",
        DEFAULT_RATE_LIMIT_MAX_DELAY_S,
        minimum=0.01,
        integer=False,
    )


def slot_timeout_s() -> float:
    """Longest a request waits for an in-flight slot (``LLM_REQUEST_SLOT_TIMEOUT_S``)."""
    return _env_number(
        "LLM_REQUEST_SLOT_TIMEOUT_S",
        DEFAULT_SLOT_TIMEOUT_S,
        minimum=0.01,
        integer=False,
    )


def embed_http_concurrency() -> int:
    """Parallel embedding requests per ``embed_texts`` call (``EMBED_HTTP_CONCURRENCY``)."""
    return int(
        _env_number(
            "EMBED_HTTP_CONCURRENCY",
            DEFAULT_EMBED_HTTP_CONCURRENCY,
            minimum=1,
            integer=True,
        )
    )


def parse_retry_after(headers: httpx.Headers) -> float | None:
    """Seconds the server asked us to wait, from ``Retry-After(-Ms)``, or ``None``."""
    raw_ms = headers.get("retry-after-ms")
    if raw_ms:
        try:
            return max(0.0, float(raw_ms) / 1000.0)
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def backoff_delay(
    attempt: int,
    *,
    base: float,
    cap: float,
    rand: Callable[[float, float], float] | None = None,
) -> float:
    """Full-jitter exponential delay before retry number ``attempt + 1``."""
    return (rand or random.uniform)(0.0, min(cap, base * (2**attempt)))


def backoff_exhausted(exc: BaseException) -> bool:
    """Whether *exc* is a 429/503 the transport already waited on until its budget ran out.

    Application-level retry loops use this to avoid starting a second budget.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    try:
        return headers is not None and headers.get(_EXHAUSTED_HEADER) == "1"
    except Exception:
        return False


class _SlotStream(httpx.SyncByteStream):
    """Wraps a response stream so the request slot is freed when it is closed."""

    def __init__(self, inner: httpx.SyncByteStream, release: Callable[[], None]):
        self._inner = inner
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def __iter__(self):
        return iter(self._inner)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            with self._lock:
                released, self._released = self._released, True
            if not released:
                self._release()


class RequestSlotTimeout(RuntimeError):
    """No in-flight request slot freed up in time (see ``LLM_MAX_CONCURRENT_REQUESTS``)."""


class GatewayClient(httpx.Client):
    """``httpx.Client`` with the process-wide request cap and 429/503 backoff.

    Subclassing the client (rather than passing a custom transport) keeps
    httpx's default environment handling: ``HTTP(S)_PROXY``, ``ALL_PROXY``,
    ``NO_PROXY`` and ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` apply exactly as they do
    for the OpenAI SDK's own client. The logic wraps ``send()``, which every
    request goes through once (redirects happen inside it).
    """

    def __init__(
        self,
        *args: object,
        slots: Callable[[], threading.BoundedSemaphore] | None = None,
        sleep: Callable[[float], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._slots = slots or request_slots
        # None resolves ``time.sleep`` at call time so tests can patch it.
        self._sleep = sleep

    def _acquire(self, slots: threading.BoundedSemaphore, host: str) -> None:
        timeout = slot_timeout_s()
        first = min(SLOT_WAIT_WARN_S, timeout)
        if slots.acquire(timeout=first):
            return
        logger.warning(
            "Waiting for a request slot to %s: %d concurrent request(s) in "
            "flight (LLM_MAX_CONCURRENT_REQUESTS).",
            host,
            max_concurrent_requests(),
        )
        if not slots.acquire(timeout=max(0.0, timeout - first)):
            raise RequestSlotTimeout(
                f"No request slot freed up within {timeout:.0f}s: every one of the "
                f"{max_concurrent_requests()} in-flight slots "
                "(LLM_MAX_CONCURRENT_REQUESTS) is held. Raise the limit, or look "
                "for a response that was never closed."
            )

    def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:  # type: ignore[override]
        budget = rate_limit_budget_s()
        base = rate_limit_base_delay_s()
        cap = rate_limit_max_delay_s()
        slots = self._slots()
        waited = 0.0
        attempt = 0
        while True:
            self._acquire(slots, request.url.host)
            try:
                response = super().send(request, **kwargs)  # type: ignore[arg-type]
            except BaseException:
                slots.release()
                raise

            if response.status_code not in RATE_LIMIT_STATUSES:
                return self._hold_slot(response, slots)

            asked = parse_retry_after(response.headers)
            delay = (
                asked
                if asked is not None
                else backoff_delay(attempt, base=base, cap=cap)
            )
            if attempt >= MAX_RATE_LIMIT_RETRIES or waited + delay > budget:
                logger.warning(
                    "HTTP %d from %s: giving up after %d retr%s and %.1fs of "
                    "backoff (budget %.0fs).",
                    response.status_code,
                    request.url.host,
                    attempt,
                    "y" if attempt == 1 else "ies",
                    waited,
                    budget,
                )
                response.headers["x-should-retry"] = "false"
                response.headers[_EXHAUSTED_HEADER] = "1"
                return self._hold_slot(response, slots)

            response.close()
            slots.release()
            logger.warning(
                "HTTP %d from %s: waiting %.1fs before retry %d/%d (%s).",
                response.status_code,
                request.url.host,
                delay,
                attempt + 1,
                MAX_RATE_LIMIT_RETRIES,
                "Retry-After" if asked is not None else "backoff",
            )
            (self._sleep or time.sleep)(delay)
            waited += delay
            attempt += 1

    @staticmethod
    def _hold_slot(
        response: httpx.Response, slots: threading.BoundedSemaphore
    ) -> httpx.Response:
        """Return *response* with its slot released when the body stream closes."""
        stream = response.stream
        # A closed response (body fully read) will not be closed again by the
        # caller, so its slot must be freed now.
        if response.is_closed or not isinstance(stream, httpx.SyncByteStream):
            slots.release()
            return response
        response.stream = _SlotStream(stream, slots.release)
        return response


_slots_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_client: GatewayClient | None = None


def request_slots() -> threading.BoundedSemaphore:
    """The process-wide semaphore bounding in-flight gateway requests."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(max_concurrent_requests())
        return _slots


def shared_http_client() -> GatewayClient:
    """The one ``httpx.Client`` for LLM, embedding and rerank calls.

    Built like the OpenAI SDK's default client (same limits, redirects and
    environment handling). Timeouts are set per request by the callers.
    """
    global _client
    with _slots_lock:
        if _client is None:
            _client = GatewayClient(
                timeout=httpx.Timeout(600.0, connect=5.0),
                limits=httpx.Limits(
                    max_connections=1000, max_keepalive_connections=100
                ),
                follow_redirects=True,
            )
        return _client
