# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The cap on concurrent chat worker subprocesses (``CHAT_MAX_CONCURRENT``).

No subprocess is ever spawned here: ``PrewarmedWorker`` is replaced by a
fake, so what is under test is the counting gate in
``auto_ontology/server/chat/limits.py``, the permit bookkeeping in
``WarmPool``, and that every path out of ``router.chat_completions`` gives
its permit back. The live behaviour (real subprocesses under real load) is
not covered — see the register row in ``docs/verification-status.md``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Iterator

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch

from auto_ontology.server.chat import router
from auto_ontology.server.chat import worker as worker_module
from auto_ontology.server.chat.conversation_dal import (
    ConversationAccessError,
    PreparedConversation,
)
from auto_ontology.server.chat.helpers import ChatRequest
from auto_ontology.server.chat.limits import ChatBusyTimeout


class _FakeWorker:
    """Stand-in for a prewarmed subprocess: cheap, synchronous, killable."""

    def __init__(self) -> None:
        self.killed = False
        self.submitted: dict[str, Any] = {}

    def is_alive(self) -> bool:
        return not self.killed

    def drain_until_ready(self, timeout: float | None = None) -> bool:
        return True

    def kill(self) -> None:
        self.killed = True

    def submit(self, question: str, **kwargs: Any) -> None:
        self.submitted = {"question": question, **kwargs}

    def events(self) -> Iterator[dict[str, Any]]:
        return iter(())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: MonkeyPatch) -> None:
    for name in ("CHAT_MAX_CONCURRENT", "CHAT_WAIT_S"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(name="pool")
def pool_fixture(monkeypatch: MonkeyPatch) -> worker_module.WarmPool:
    """A real ``WarmPool`` over fake workers, with no background spawns.

    ``_spawn_async`` is disabled so construction and the standby logic stay
    deterministic; the standby-spawn path has its own test below.
    """
    monkeypatch.setattr(worker_module, "PrewarmedWorker", _FakeWorker)
    monkeypatch.setattr(worker_module.WarmPool, "_spawn_async", lambda self: None)
    return worker_module.WarmPool()


# -- knobs -----------------------------------------------------------------


def test_defaults() -> None:
    from auto_ontology.server.chat import limits

    assert limits.max_concurrent() == limits.DEFAULT_MAX_CONCURRENT == 4
    assert limits.wait_s() == limits.DEFAULT_WAIT_S == 120.0


def test_env_overrides(monkeypatch: MonkeyPatch) -> None:
    from auto_ontology.server.chat import limits

    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "9")
    monkeypatch.setenv("CHAT_WAIT_S", "1.5")

    assert limits.max_concurrent() == 9
    assert limits.wait_s() == 1.5


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "nan", "inf", ""])
def test_a_bad_value_falls_back_to_the_default_and_never_disables_the_cap(
    monkeypatch: MonkeyPatch, bad: str
) -> None:
    from auto_ontology.server.chat import limits

    monkeypatch.setenv("CHAT_MAX_CONCURRENT", bad)
    monkeypatch.setenv("CHAT_WAIT_S", bad)

    assert limits.max_concurrent() == limits.DEFAULT_MAX_CONCURRENT
    assert limits.wait_s() == limits.DEFAULT_WAIT_S


# -- the gate --------------------------------------------------------------


def test_the_cap_holds_and_surplus_requests_wait(pool: worker_module.WarmPool) -> None:
    """N+k acquires against a cap of N: never more than N handed out, and
    the surplus succeed as permits come back rather than failing."""
    cap, extra, hold_s = 3, 3, 0.05

    held = 0
    peak = 0
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Event()

    def work() -> None:
        nonlocal held, peak
        start.wait(5)  # all N+k ask at once, like a real burst
        try:
            w = pool.acquire()
            with lock:
                held += 1
                peak = max(peak, held)
            time.sleep(hold_s)
            pool.return_alive(w)
            with lock:
                held -= 1
        except BaseException as exc:  # noqa: BLE001 - returned to the test
            with lock:
                errors.append(exc)

    with MonkeyPatch.context() as mp:
        mp.setenv("CHAT_MAX_CONCURRENT", str(cap))
        mp.setenv("CHAT_WAIT_S", "30")
        threads = [threading.Thread(target=work) for _ in range(cap + extra)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

    assert errors == []
    assert peak == cap
    assert pool.slots_in_use() == 0


def test_timeout_is_a_clear_error_that_names_the_knobs(
    monkeypatch: MonkeyPatch, pool: worker_module.WarmPool
) -> None:
    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "1")
    monkeypatch.setenv("CHAT_WAIT_S", "0.2")

    held = pool.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(ChatBusyTimeout) as caught:
            pool.acquire()
        assert 0.15 <= time.monotonic() - started < 2
    finally:
        pool.return_alive(held)

    message = str(caught.value)
    assert "chat slots are busy" in message
    assert "CHAT_MAX_CONCURRENT" in message and "CHAT_WAIT_S" in message
    assert pool.slots_in_use() == 0  # the failed wait leaked no permit


def test_a_spawn_failure_gives_the_permit_straight_back(
    monkeypatch: MonkeyPatch, pool: worker_module.WarmPool
) -> None:
    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "1")

    def explode() -> None:
        raise OSError("cannot fork")

    monkeypatch.setattr(worker_module, "PrewarmedWorker", explode)
    with pytest.raises(OSError):
        pool.acquire()

    assert pool.slots_in_use() == 0
    # The pool is usable again once spawning works once more.
    monkeypatch.setattr(worker_module, "PrewarmedWorker", _FakeWorker)
    pool.return_alive(pool.acquire())
    assert pool.slots_in_use() == 0


def test_a_cancelled_worker_releases_its_permit(
    monkeypatch: MonkeyPatch, pool: worker_module.WarmPool
) -> None:
    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "1")

    w = pool.acquire()
    assert pool.slots_in_use() == 1
    pool.replace(w)
    assert w.killed
    assert pool.slots_in_use() == 0

    pool.return_alive(pool.acquire())  # free for the next request
    assert pool.slots_in_use() == 0


def test_the_standby_spawn_takes_no_permit(monkeypatch: MonkeyPatch) -> None:
    """``CHAT_MAX_CONCURRENT`` bounds *requests*; the warm standby is extra,
    so building it (and refreshing it) must leave the gate untouched."""
    spawned: list[_FakeWorker] = []
    real_init = _FakeWorker.__init__

    def counting_init(self: _FakeWorker) -> None:
        real_init(self)
        spawned.append(self)

    monkeypatch.setattr(worker_module, "PrewarmedWorker", _FakeWorker)
    monkeypatch.setattr(_FakeWorker, "__init__", counting_init)
    pool = worker_module.WarmPool()
    # refresh_chat_workers() goes through the module-level pool.
    monkeypatch.setattr(worker_module, "_pool", pool)

    deadline = time.monotonic() + 5
    while pool._standby is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pool._standby is not None
    assert pool.slots_in_use() == 0

    worker_module.refresh_chat_workers()
    deadline = time.monotonic() + 5
    while pool._standby is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pool._standby is not None
    assert pool.slots_in_use() == 0

    # A warm standby is handed out as-is: no extra subprocess for the request.
    before = len(spawned)
    pool.acquire()
    assert len(spawned) == before
    assert pool.slots_in_use() == 1
    pool.shutdown()


# -- the router: every exit gives the permit back --------------------------


class _RouterHarness:
    """``chat_completions`` with everything outside the chat module faked.

    The pool is a real ``WarmPool`` over fake workers, so permits are really
    taken and really counted.
    """

    def __init__(self, monkeypatch: MonkeyPatch, pool: worker_module.WarmPool) -> None:
        self.pool = pool
        self.prepare_error: Exception | None = None
        monkeypatch.setattr(router, "get_pool", lambda: pool)
        monkeypatch.setattr(router, "semantic_layer_calculated", lambda: True)
        monkeypatch.setattr(router, "_subject_token", lambda _r: None)
        monkeypatch.setattr(
            router, "resolve_internal_user", lambda _r, required=False: "user"
        )
        monkeypatch.setattr(router, "prepare_conversation", self._prepare)
        monkeypatch.setattr(router, "is_clarification_enabled", lambda: False)
        monkeypatch.setattr(router, "create_stateless_analytics", lambda **kw: "a")

    def _prepare(self, **kwargs: Any) -> PreparedConversation:
        if self.prepare_error is not None:
            raise self.prepare_error
        return PreparedConversation(history=[], analytics_id="a")

    def post(self, **body: Any) -> dict[str, Any]:
        request = ChatRequest(**body)

        class _Http:
            headers: dict[str, str] = {}

            async def is_disconnected(self) -> bool:
                return False

        outcome: dict[str, Any] = {}

        async def run() -> None:
            try:
                outcome["response"] = await router.chat_completions(
                    request,
                    _Http(),  # type: ignore[arg-type]
                )
            except BaseException as exc:  # noqa: BLE001 - returned to the test
                outcome["error"] = exc
            await asyncio.sleep(0.05)  # let the watchdog and pump settle

        asyncio.run(run())
        return outcome


@pytest.fixture(name="harness")
def harness_fixture(
    monkeypatch: MonkeyPatch, pool: worker_module.WarmPool
) -> _RouterHarness:
    router._active_slots.clear()
    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "4")
    return _RouterHarness(monkeypatch, pool)


def _drain(pool: worker_module.WarmPool) -> None:
    """Wait for every pump thread to finish and release its slot."""
    deadline = time.monotonic() + 5
    while pool.slots_in_use() > 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pool.slots_in_use() == 0


def test_a_normal_request_comes_back_to_zero(harness: _RouterHarness) -> None:
    conversation = uuid.uuid4()
    outcome = harness.post(question="how many orders?", conversation_id=conversation)
    assert "error" not in outcome
    _drain(harness.pool)


def test_a_stateless_request_comes_back_to_zero(harness: _RouterHarness) -> None:
    outcome = harness.post(question="how many orders?")
    assert "error" not in outcome
    _drain(harness.pool)


def test_a_404_prepare_failure_comes_back_to_zero(harness: _RouterHarness) -> None:
    harness.prepare_error = ConversationAccessError("nope")
    outcome = harness.post(question="q", conversation_id=uuid.uuid4())
    assert isinstance(outcome["error"], HTTPException)
    assert outcome["error"].status_code == 404
    assert harness.pool.slots_in_use() == 0


def test_the_409_sibling_race_comes_back_to_zero(harness: _RouterHarness) -> None:
    conversation = uuid.uuid4()
    live = router._Slot(
        key=f"user:{conversation}",
        worker=_FakeWorker(),  # type: ignore[arg-type]
        user_id="user",
        conversation_id=conversation,
        analytics_id=None,
        question="running",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
    )
    live.client_alive.set()
    router._active_slots[live.key] = live

    outcome = harness.post(question="q", conversation_id=conversation)

    assert isinstance(outcome["error"], HTTPException)
    assert outcome["error"].status_code == 409
    assert harness.pool.slots_in_use() == 0


def test_preempting_an_orphan_releases_both_permits(harness: _RouterHarness) -> None:
    conversation = uuid.uuid4()
    orphan_worker = harness.pool.acquire()  # the orphan holds a real permit
    orphan = router._Slot(
        key=f"user:{conversation}",
        worker=orphan_worker,  # type: ignore[arg-type]
        user_id="user",
        conversation_id=conversation,
        analytics_id=None,
        question="orphaned",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
    )
    # Orphan: its client is gone but the slot is still installed.
    router._active_slots[orphan.key] = orphan
    assert harness.pool.slots_in_use() == 1

    outcome = harness.post(
        question="a genuinely new question", conversation_id=conversation
    )
    assert "error" not in outcome
    # The new request's permit plus the orphan's both come back.
    _drain(harness.pool)


def test_the_cancel_route_releases_the_permit(harness: _RouterHarness) -> None:
    conversation = uuid.uuid4()

    class _Request:
        headers: dict[str, str] = {}

    w = harness.pool.acquire()
    slot = router._Slot(
        key=f"user:{conversation}",
        worker=w,  # type: ignore[arg-type]
        user_id="user",
        conversation_id=conversation,
        analytics_id=None,
        question="running",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
    )
    slot.client_alive.set()
    router._active_slots[slot.key] = slot
    assert harness.pool.slots_in_use() == 1

    result = asyncio.run(router.chat_cancel(conversation, _Request()))  # type: ignore[arg-type]

    assert result == {"cancelled": True}
    assert harness.pool.slots_in_use() == 0
    assert w.killed


def test_a_double_release_cannot_inflate_capacity(
    harness: _RouterHarness,
) -> None:
    """``_release`` is idempotent: calling it twice must return exactly one
    permit, or every double release would silently raise the cap."""
    w = harness.pool.acquire()
    slot = router._Slot(
        key="user:conv",
        worker=w,  # type: ignore[arg-type]
        user_id="user",
        conversation_id=uuid.uuid4(),
        analytics_id=None,
        question="q",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
    )
    router._release(slot)
    router._release(slot)

    assert harness.pool.slots_in_use() == 0

    # Capacity is unchanged: with the cap at 4, exactly four acquires still
    # fit and the fifth waits rather than succeeding instantly.
    with MonkeyPatch.context() as mp:
        mp.setenv("CHAT_WAIT_S", "0.2")
        held = [harness.pool.acquire() for _ in range(4)]
        with pytest.raises(ChatBusyTimeout):
            harness.pool.acquire()
        for w in held:
            harness.pool.return_alive(w)
    assert harness.pool.slots_in_use() == 0


# -- the 503 and the disconnect-while-waiting paths ------------------------


def test_the_router_maps_the_timeout_to_503_with_retry_after(
    monkeypatch: MonkeyPatch,
) -> None:
    def busy(cancel: Any = None) -> Any:
        raise ChatBusyTimeout(
            "All 4 chat slots are busy; waited 120s. Retry shortly. "
            "Operators: raise CHAT_MAX_CONCURRENT / CHAT_WAIT_S if this keeps "
            "happening."
        )

    pool = type("_P", (), {"acquire": staticmethod(busy)})()
    monkeypatch.setattr(router, "get_pool", lambda: pool)
    monkeypatch.setattr(router, "semantic_layer_calculated", lambda: True)
    monkeypatch.setattr(router, "_subject_token", lambda _r: None)
    monkeypatch.setattr(
        router, "resolve_internal_user", lambda _r, required=False: "user"
    )

    request = ChatRequest(question="q")
    outcome: dict[str, Any] = {}

    class _Http:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return False

    async def run() -> None:
        try:
            await router.chat_completions(request, _Http())  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - returned to the test
            outcome["error"] = exc

    asyncio.run(run())

    error = outcome["error"]
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert error.headers is not None
    assert error.headers["Retry-After"] == router._RETRY_AFTER_S
    assert "CHAT_MAX_CONCURRENT" in error.detail


def test_a_client_that_vanished_while_waiting_gets_no_run(
    harness: _RouterHarness,
) -> None:
    """The permit goes straight back; no question is submitted for a client
    that disconnected during the wait."""
    request = ChatRequest(question="q")
    returned: list[Any] = []
    real_return_alive = harness.pool.return_alive

    def record_return(worker: Any) -> None:
        returned.append(worker)
        real_return_alive(worker)

    harness.pool.return_alive = record_return  # type: ignore[method-assign]
    outcome: dict[str, Any] = {}

    class _Http:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return True

    async def run() -> None:
        outcome["response"] = await router.chat_completions(
            request,
            _Http(),  # type: ignore[arg-type]
        )

    asyncio.run(run())

    assert "response" in outcome  # a closed stream, not an error
    # Either the wait was abandoned (no worker taken at all) or the worker was
    # taken and handed straight back; never is a question submitted.
    assert all(not w.submitted for w in returned)
    assert harness.pool.slots_in_use() == 0


def test_waiters_do_not_starve_the_default_executor(monkeypatch: MonkeyPatch) -> None:
    """Requests parked for a slot must not occupy asyncio's default executor.

    That executor is shared with the steps the slot *holders* still run
    (``prepare_conversation`` and friends). If waiters filled it, no holder could
    finish, no slot would be released, and every waiter would time out together.
    """
    release = threading.Event()

    def blocked_acquire() -> Any:
        release.wait(10)
        raise ChatBusyTimeout("busy")

    pool = type("_P", (), {"acquire": staticmethod(blocked_acquire)})()
    monkeypatch.setattr(router, "get_pool", lambda: pool)
    monkeypatch.setattr(router, "semantic_layer_calculated", lambda: True)
    monkeypatch.setattr(router, "_subject_token", lambda _r: None)
    monkeypatch.setattr(router, "_resolve_chat_target_db", lambda target_db: None)
    monkeypatch.setattr(
        router, "resolve_internal_user", lambda _r, required=False: "user"
    )

    class _Http:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return False

    async def run() -> float:
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        waiters = [
            asyncio.create_task(
                router.chat_completions(ChatRequest(question="q"), _Http())  # type: ignore[arg-type]
            )
            for _ in range(6)  # three times the default executor's size
        ]
        await asyncio.sleep(0.3)  # let every waiter park on its slot wait
        started = time.monotonic()
        # What a slot holder does next: it must not queue behind the waiters.
        await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=2)
        elapsed = time.monotonic() - started
        release.set()
        await asyncio.gather(*waiters, return_exceptions=True)
        return elapsed

    assert asyncio.run(run()) < 1.0
