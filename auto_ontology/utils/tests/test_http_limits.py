# SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
# SPDX-License-Identifier: Apache-2.0

"""Rate-limit backoff and the process-wide request cap, with no network.

The gateway is an ``httpx.MockTransport`` under the real transport wrapper (and,
where it matters, under the real OpenAI client); ``time.sleep`` is replaced so
the delays can be asserted instead of waited for.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import httpx
import pytest
from openai import OpenAI, RateLimitError

from auto_ontology.retrieval.data_access import semantic_search
from auto_ontology.utils import http_limits
from auto_ontology.utils.http_limits import (
    GatewayClient,
    RequestSlotTimeout,
    backoff_exhausted,
    parse_retry_after,
)
from auto_ontology.utils.llm_invoke import _is_retryable

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLM_MAX_CONCURRENT_REQUESTS",
        "LLM_RATE_LIMIT_BUDGET_S",
        "LLM_RATE_LIMIT_BASE_DELAY_S",
        "LLM_RATE_LIMIT_MAX_DELAY_S",
        "EMBED_HTTP_CONCURRENCY",
        "LLM_REQUEST_SLOT_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


class _Gateway:
    def __init__(self, handler: Handler, *, slots: int = 8) -> None:
        self.sleeps: list[float] = []
        self.requests = 0
        self.semaphore = threading.BoundedSemaphore(slots)
        self._handler = handler
        self.client = GatewayClient(
            transport=httpx.MockTransport(self._handle),
            slots=lambda: self.semaphore,
            sleep=self.sleeps.append,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self._handler(request)


def _limited(headers: dict[str, str] | None = None, status: int = 429):
    return httpx.Response(status, headers=headers or {}, json={"error": "slow"})


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _script(*responses: httpx.Response) -> Handler:
    queue = list(responses)
    return lambda request: queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def max_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make full jitter return its upper bound, so delays are predictable."""
    monkeypatch.setattr(http_limits.random, "uniform", lambda low, high: high)


def test_backoff_doubles_with_jitter_and_stops_at_the_budget(max_jitter) -> None:
    gateway = _Gateway(_script(_limited()))  # 429 forever
    response = gateway.client.post("http://gw/v1/x", json={})
    # Upper bounds 2, 4, 8, 16, 30 (capped) = 60s; the next 30s would pass 60s.
    assert gateway.sleeps == [2, 4, 8, 16, 30]
    assert gateway.requests == 6
    assert response.status_code == 429
    assert response.headers["x-should-retry"] == "false"
    assert response.headers["x-lexis-backoff-exhausted"] == "1"


def test_jitter_is_full_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float, float]] = []

    def _uniform(low: float, high: float) -> float:
        seen.append((low, high))
        return low

    monkeypatch.setattr(http_limits.random, "uniform", _uniform)
    gateway = _Gateway(_script(_limited(), _limited(), _ok()))
    assert gateway.client.post("http://gw/x").status_code == 200
    assert seen == [(0.0, 2.0), (0.0, 4.0)]
    assert gateway.sleeps == [0.0, 0.0]


def test_retry_after_seconds_is_honoured(max_jitter) -> None:
    gateway = _Gateway(_script(_limited({"retry-after": "7"}), _ok()))
    assert gateway.client.post("http://gw/x").status_code == 200
    assert gateway.sleeps == [7.0]
    assert gateway.requests == 2


def test_retry_after_ms_and_http_date_are_understood() -> None:
    assert parse_retry_after(httpx.Headers({"retry-after-ms": "1500"})) == 1.5
    future = time.time() + 20
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))
    parsed = parse_retry_after(httpx.Headers({"retry-after": date}))
    assert parsed is not None and 15 < parsed <= 20
    assert parse_retry_after(httpx.Headers({"retry-after": "soon"})) is None
    assert parse_retry_after(httpx.Headers()) is None


def test_retry_after_beyond_the_budget_gives_up_without_waiting() -> None:
    gateway = _Gateway(_script(_limited({"retry-after": "600"})))
    response = gateway.client.post("http://gw/x")
    assert gateway.sleeps == []
    assert gateway.requests == 1
    assert response.status_code == 429


def test_a_503_is_treated_like_a_429(max_jitter) -> None:
    gateway = _Gateway(_script(_limited(status=503), _ok()))
    assert gateway.client.post("http://gw/x").status_code == 200
    assert gateway.sleeps == [2.0]


@pytest.mark.parametrize("status", [200, 400, 401, 404, 500, 502, 504])
def test_other_statuses_are_not_retried_here(status: int) -> None:
    gateway = _Gateway(_script(httpx.Response(status, json={})))
    assert gateway.client.post("http://gw/x").status_code == status
    assert gateway.sleeps == []
    assert gateway.requests == 1


def test_transport_errors_propagate_and_free_the_slot() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    gateway = _Gateway(_boom, slots=1)
    with pytest.raises(httpx.ConnectError):
        gateway.client.post("http://gw/x")
    assert gateway.semaphore.acquire(blocking=False)  # slot was released


def test_the_budget_is_an_env_knob(monkeypatch: pytest.MonkeyPatch, max_jitter) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_BUDGET_S", "5")
    gateway = _Gateway(_script(_limited()))
    gateway.client.post("http://gw/x")
    # Bounds are 2 then 4; 2 + 4 would pass the 5s budget, so it stops after one wait.
    assert gateway.sleeps == [2]


def test_the_slot_is_free_while_waiting_to_retry(max_jitter) -> None:
    gateway = _Gateway(_script(_limited(), _ok()), slots=1)
    free_during_wait: list[bool] = []

    def _sleep(delay: float) -> None:
        free_during_wait.append(gateway.semaphore.acquire(blocking=False))
        if free_during_wait[-1]:
            gateway.semaphore.release()

    gateway.client._sleep = _sleep
    assert gateway.client.post("http://gw/x").status_code == 200
    assert free_during_wait == [True]


def test_the_slot_is_held_until_the_body_is_read() -> None:
    unread = httpx.Response(200, stream=httpx.ByteStream(b"body"))
    gateway = _Gateway(_script(unread), slots=1)
    with gateway.client.stream("GET", "http://gw/x") as response:
        assert not gateway.semaphore.acquire(blocking=False)
        response.read()
    assert gateway.semaphore.acquire(blocking=False)


# -- the OpenAI SDK on top of it: real HTTP attempts per logical call ---------


def test_sdk_retries_do_not_multiply_the_attempts(max_jitter) -> None:
    gateway = _Gateway(_script(_limited()))
    client = OpenAI(
        base_url="http://gw/v1",
        api_key="k",
        max_retries=5,  # what the embedding client is configured with
        http_client=gateway.client,
    )
    with pytest.raises(RateLimitError) as caught:
        client.embeddings.create(model="m", input=["a"])
    # 1 request + 5 transport retries; before, the SDK alone made 6 in ~15s with
    # no budget, and the callers' loops multiplied that again.
    assert gateway.requests == 6
    assert backoff_exhausted(caught.value)
    assert not _is_retryable(caught.value)
    assert not semantic_search._is_retryable(caught.value)


def test_sdk_still_retries_other_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    gateway = _Gateway(_script(httpx.Response(502, json={}), _ok()))
    client = OpenAI(
        base_url="http://gw/v1", api_key="k", max_retries=2, http_client=gateway.client
    )
    client.post("/x", cast_to=object, body={})
    assert gateway.requests == 2  # 502 retried once by the SDK, as before


def test_plain_rate_limit_errors_without_a_response_keep_their_old_handling() -> None:
    assert _is_retryable(RuntimeError("429 Too Many Requests"))
    assert not backoff_exhausted(RuntimeError("429"))


# -- the cap ------------------------------------------------------------------


def test_the_cap_is_never_exceeded_under_many_threads() -> None:
    cap = 3
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.005)
        with lock:
            state["now"] -= 1
        return _ok()

    gateway = _Gateway(_handler, slots=cap)
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            for _ in range(5):
                assert gateway.client.get("http://gw/x").status_code == 200
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_work) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert 1 <= state["peak"] <= cap
    assert gateway.semaphore.acquire(blocking=False)  # nothing leaked


def test_the_cap_comes_from_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_limits, "_slots", None)
    assert http_limits.request_slots()._value == 8  # type: ignore[attr-defined]
    monkeypatch.setattr(http_limits, "_slots", None)
    monkeypatch.setenv("LLM_MAX_CONCURRENT_REQUESTS", "2")
    assert http_limits.request_slots()._value == 2  # type: ignore[attr-defined]
    monkeypatch.setattr(http_limits, "_slots", None)


def test_embed_concurrency_defaults_to_4_and_is_an_env_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_ontology.utils.embedding import EmbedParams

    assert EmbedParams("m", "http://e").http_max_concurrent == 4
    monkeypatch.setenv("EMBED_HTTP_CONCURRENCY", "16")
    assert EmbedParams("m", "http://e").http_max_concurrent == 16


# -- invalid env values -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reader", "default"),
    [
        ("LLM_MAX_CONCURRENT_REQUESTS", http_limits.max_concurrent_requests, 8),
        ("LLM_RATE_LIMIT_BUDGET_S", http_limits.rate_limit_budget_s, 60.0),
        ("LLM_RATE_LIMIT_BASE_DELAY_S", http_limits.rate_limit_base_delay_s, 2.0),
        ("LLM_RATE_LIMIT_MAX_DELAY_S", http_limits.rate_limit_max_delay_s, 30.0),
        ("EMBED_HTTP_CONCURRENCY", http_limits.embed_http_concurrency, 4),
        ("LLM_REQUEST_SLOT_TIMEOUT_S", http_limits.slot_timeout_s, 300.0),
    ],
)
@pytest.mark.parametrize("bad", ["abc", "0", "-3", "nan", "2.5x"])
def test_invalid_values_fall_back_to_the_default_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    reader: Callable[[], float],
    default: float,
    bad: str,
) -> None:
    if name == "LLM_RATE_LIMIT_BUDGET_S" and bad == "0":
        pytest.skip("0 is a valid budget (never wait)")
    monkeypatch.setenv(name, bad)
    with caplog.at_level(logging.WARNING, logger=http_limits.logger.name):
        assert reader() == default
    assert name in caplog.text


def test_a_zero_budget_never_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_RATE_LIMIT_BUDGET_S", "0")
    gateway = _Gateway(_script(_limited()))
    assert gateway.client.post("http://gw/x").status_code == 429
    assert gateway.sleeps == [] and gateway.requests == 1


def test_each_wait_is_logged_once_at_warning(
    caplog: pytest.LogCaptureFixture, max_jitter
) -> None:
    gateway = _Gateway(_script(_limited(), _limited(), _ok()))
    with caplog.at_level(logging.WARNING, logger=http_limits.logger.name):
        gateway.client.post("http://gw/x")
    waits = [r for r in caplog.records if "waiting" in r.getMessage()]
    assert len(waits) == 2
    assert all(r.levelno == logging.WARNING for r in waits)


# -- slot timeout -------------------------------------------------------------


def test_a_leaked_slot_times_out_with_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LLM_REQUEST_SLOT_TIMEOUT_S", "0.3")
    monkeypatch.setattr(http_limits, "SLOT_WAIT_WARN_S", 0.1)
    unread = httpx.Response(200, stream=httpx.ByteStream(b"body"))
    gateway = _Gateway(_script(unread, _ok()), slots=1)
    leaked = gateway.client.send(
        gateway.client.build_request("GET", "http://gw/x"), stream=True
    )  # never closed: holds the only slot
    with caplog.at_level(logging.WARNING, logger=http_limits.logger.name):
        with pytest.raises(RequestSlotTimeout, match="LLM_MAX_CONCURRENT_REQUESTS"):
            gateway.client.get("http://gw/y")
    assert "Waiting for a request slot" in caplog.text
    leaked.close()
    assert gateway.client.get("http://gw/y").status_code == 200  # recovered


def test_a_short_wait_for_a_slot_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = _Gateway(_script(_ok()), slots=2)
    with caplog.at_level(logging.WARNING, logger=http_limits.logger.name):
        gateway.client.get("http://gw/x")
    assert "Waiting for a request slot" not in caplog.text


# -- environment handling is inherited from httpx -----------------------------


def test_env_proxies_and_no_proxy_are_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    monkeypatch.setattr(http_limits, "_client", None)
    try:
        client = http_limits.shared_http_client()
        assert isinstance(client, GatewayClient)
        proxied = client._transport_for_url(httpx.URL("https://gateway.example.com/v1"))
        direct = client._transport_for_url(httpx.URL("https://internal.corp/v1"))
        assert proxied is not direct
        assert proxied._pool._proxy_url.host == b"proxy.corp"  # type: ignore[attr-defined]
        assert not hasattr(direct._pool, "_proxy_url")  # type: ignore[attr-defined]
    finally:
        monkeypatch.setattr(http_limits, "_client", None)


def test_env_ca_bundle_is_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import certifi

    bundle = tmp_path / "ca.pem"
    bundle.write_text(open(certifi.where(), encoding="utf-8").read())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setattr(http_limits, "_client", None)
    loaded: list[str] = []
    import ssl

    real = ssl.SSLContext.load_verify_locations

    def _spy(self, cafile=None, *args, **kwargs):
        loaded.append(str(cafile))
        return real(self, cafile, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", _spy)
    try:
        http_limits.shared_http_client()
    finally:
        monkeypatch.setattr(http_limits, "_client", None)
    assert str(bundle) in loaded
