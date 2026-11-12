# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Orphaned and over-long chat runs get cancelled (``CHAT_ORPHAN_GRACE_S``,
``CHAT_MAX_RUN_S``).

No subprocess and no network: workers are fakes whose ``events()`` blocks
until ``kill()``, like a real agent stuck in a long LLM call. The pool is a
real ``WarmPool`` so permits are really taken and counted. What is NOT
covered here is a real client TCP disconnect through uvicorn (the fake
request just flips a flag that ``is_disconnected()`` reports) and real
agent subprocesses; see ``docs/verification-status.md``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any, Callable, Iterator

import pytest
from pytest import MonkeyPatch

from auto_ontology.server.chat import router
from auto_ontology.server.chat import worker as worker_module
from auto_ontology.server.chat.conversation_dal import PreparedConversation
from auto_ontology.server.chat.helpers import ChatRequest


class _BlockingWorker:
    """Runs until killed, like an agent mid-way through a long question."""

    spawned: list["_BlockingWorker"] = []

    def __init__(self) -> None:
        self.killed = False
        _BlockingWorker.spawned.append(self)

    def is_alive(self) -> bool:
        return not self.killed

    def drain_until_ready(self, timeout: float | None = None) -> bool:
        return True

    def kill(self) -> None:
        self.killed = True

    def submit(self, question: str, **kwargs: Any) -> None:
        pass

    def events(self) -> Iterator[dict[str, Any] | None]:
        while not self.killed:
            time.sleep(0.01)
            yield None


class _Http:
    """The slice of ``starlette.Request`` the router uses."""

    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class _Env:
    def __init__(self, monkeypatch: MonkeyPatch) -> None:
        _BlockingWorker.spawned = []
        router._active_slots.clear()
        monkeypatch.setattr(worker_module, "PrewarmedWorker", _BlockingWorker)
        monkeypatch.setattr(worker_module.WarmPool, "_spawn_async", lambda self: None)
        self.pool = worker_module.WarmPool()
        self.persisted: list[dict[str, Any]] = []
        monkeypatch.setattr(router, "get_pool", lambda: self.pool)
        monkeypatch.setattr(router, "semantic_layer_calculated", lambda: True)
        monkeypatch.setattr(router, "_subject_token", lambda _r: None)
        monkeypatch.setattr(
            router, "resolve_internal_user", lambda _r, required=False: "user"
        )
        monkeypatch.setattr(
            router,
            "prepare_conversation",
            lambda **kw: PreparedConversation(history=[], analytics_id="a"),
        )
        monkeypatch.setattr(router, "is_clarification_enabled", lambda: False)
        monkeypatch.setattr(router, "create_stateless_analytics", lambda **kw: "a")
        monkeypatch.setattr(
            router, "persist_assistant_result", lambda **kw: self.persisted.append(kw)
        )
        monkeypatch.setattr(
            router, "persist_analytics_result", lambda **kw: self.persisted.append(kw)
        )
        monkeypatch.setenv("CHAT_MAX_CONCURRENT", "4")
        monkeypatch.delenv("CHAT_WAIT_S", raising=False)
        monkeypatch.delenv("CHAT_ORPHAN_GRACE_S", raising=False)
        monkeypatch.delenv("CHAT_MAX_RUN_S", raising=False)


@pytest.fixture(name="env")
def env_fixture(monkeypatch: MonkeyPatch) -> _Env:
    return _Env(monkeypatch)


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def _collect(response: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if line.startswith("data:") and "[DONE]" not in line:
                events.append(json.loads(line[5:]))
    return events


def _in_use_zero(env: _Env) -> Callable[[], bool]:
    return lambda: env.pool.slots_in_use() == 0


def test_stateless_disconnect_kills_the_worker_and_returns_the_permit(
    env: _Env,
) -> None:
    async def scenario() -> None:
        http = _Http()
        await router.chat_completions(ChatRequest(question="q"), http)  # type: ignore[arg-type]
        worker = _BlockingWorker.spawned[-1]
        assert env.pool.slots_in_use() == 1
        await asyncio.sleep(0.3)
        assert not worker.killed  # client still connected
        http.disconnected = True
        assert await _until(lambda: worker.killed)
        assert await _until(_in_use_zero(env))

    asyncio.run(scenario())


def test_conversation_survives_within_grace_then_is_cancelled(
    env: _Env, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_ORPHAN_GRACE_S", "1.0")

    async def scenario() -> None:
        http = _Http()
        await router.chat_completions(
            ChatRequest(question="q", conversation_id=uuid.uuid4()),
            http,  # type: ignore[arg-type]
        )
        worker = _BlockingWorker.spawned[-1]
        http.disconnected = True
        await asyncio.sleep(0.5)
        assert not worker.killed and env.pool.slots_in_use() == 1
        assert await _until(lambda: worker.killed, timeout=4)
        assert await _until(_in_use_zero(env))
        # the conversation records why the turn has no answer
        assert env.persisted and "CHAT_ORPHAN_GRACE_S" in env.persisted[0]["response"]

    asyncio.run(scenario())


def test_reattaching_within_grace_keeps_the_run_alive(
    env: _Env, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_ORPHAN_GRACE_S", "0.8")
    conversation = uuid.uuid4()

    async def scenario() -> None:
        http = _Http()
        await router.chat_completions(
            ChatRequest(question="q", conversation_id=conversation),
            http,  # type: ignore[arg-type]
        )
        worker = _BlockingWorker.spawned[-1]
        http.disconnected = True
        await asyncio.sleep(0.3)
        watcher = _Http()
        await router.chat_watch(conversation, watcher)  # type: ignore[arg-type]
        await asyncio.sleep(1.5)  # well past the grace period
        assert not worker.killed and env.pool.slots_in_use() == 1
        watcher.disconnected = True  # reader leaves: the countdown starts anew
        assert await _until(lambda: worker.killed, timeout=4)
        assert await _until(_in_use_zero(env))

    asyncio.run(scenario())


def test_max_run_time_sends_an_error_event_kills_and_records_a_failed_turn(
    env: _Env, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_MAX_RUN_S", "0.4")

    async def scenario() -> list[dict[str, Any]]:
        response = await router.chat_completions(
            ChatRequest(question="q", conversation_id=uuid.uuid4()),
            _Http(),  # type: ignore[arg-type]
        )
        worker = _BlockingWorker.spawned[-1]
        events = await asyncio.wait_for(_collect(response), timeout=5)
        assert worker.killed
        assert await _until(_in_use_zero(env))
        return events

    events = asyncio.run(scenario())
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "exceeded the CHAT_MAX_RUN_S limit" in errors[0]["message"]
    assert len(env.persisted) == 1
    assert "CHAT_MAX_RUN_S" in env.persisted[0]["response"]


def test_a_client_that_leaves_while_queued_never_takes_a_worker(
    env: _Env, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_MAX_CONCURRENT", "1")
    held = env.pool.acquire()
    spawned_before = len(_BlockingWorker.spawned)

    async def scenario() -> list[dict[str, Any]]:
        http = _Http()
        http.disconnected = True
        response = await asyncio.wait_for(
            router.chat_completions(ChatRequest(question="q"), http),  # type: ignore[arg-type]
            timeout=5,
        )
        return await _collect(response)

    assert asyncio.run(scenario()) == []
    assert env.pool.slots_in_use() == 1  # only the permit the test holds
    assert len(_BlockingWorker.spawned) == spawned_before
    env.pool.replace(held)  # type: ignore[arg-type]
    assert env.pool.slots_in_use() == 0


def test_supervise_step_respects_the_clock(env: _Env, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_ORPHAN_GRACE_S", "10")
    monkeypatch.setenv("CHAT_MAX_RUN_S", "100")
    slot = router._Slot(
        key="k",
        worker=_BlockingWorker(),  # type: ignore[arg-type]
        user_id="user",
        conversation_id=uuid.uuid4(),
        analytics_id=None,
        question="q",
        client_alive=threading.Event(),
        cancelled=threading.Event(),
        released=threading.Event(),
        started=0.0,
    )
    assert router._supervise_step(slot, 1.0) is None  # orphan clock starts
    assert router._supervise_step(slot, 10.0) is None
    reason = router._supervise_step(slot, 11.5)
    assert reason is not None and reason[0] == "orphaned"
    slot.client_alive.set()
    assert router._supervise_step(slot, 50.0) is None
    reason = router._supervise_step(slot, 101.0)
    assert reason is not None and reason[0] == "max_run_time"
