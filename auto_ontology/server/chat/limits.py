# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounds on the chat worker subprocesses this backend process runs.

Each chat conversation runs in its own agent subprocess (see
:mod:`auto_ontology.server.chat.worker`): a full Python process that imports
the agent and opens its own database pools. With no cap, a burst of
concurrent conversations cold-started one subprocess per request and could
exhaust the host (18 simultaneous conversations took the machine down).

Two knobs, both read at call time (so a test or an operator can change them
without restarting), mirroring :mod:`auto_ontology.connectors.source_limits`:

``CHAT_MAX_CONCURRENT`` (default 4)
    Conversations running at once in this backend process — the number of
    worker subprocesses ``WarmPool.acquire()`` may hand out. The single warm
    standby subprocess is extra and never takes a permit, so at most
    ``CHAT_MAX_CONCURRENT + 1`` worker subprocesses exist in steady state
    (briefly one more while a cancelled worker is being killed and its
    replacement is starting).

``CHAT_WAIT_S`` (default 120)
    How long a request waits for a free slot before failing with
    :class:`ChatBusyTimeout` (HTTP 503 with ``Retry-After`` from the router).
    Waiting is the point: under a burst the cap should queue work, not turn
    it into errors — and not into subprocesses.

``CHAT_ORPHAN_GRACE_S`` (default 60)
    How long a *conversation* run (one with a ``conversation_id``) keeps
    running after its client disconnected and nobody reattached through
    ``/chat/watch``; after that it is cancelled (worker killed, permit
    released). A stateless run (no ``conversation_id``) has no way to be
    reattached, so it is cancelled as soon as its client disconnects.

``CHAT_MAX_RUN_S`` (default 300)
    Wall-clock limit for one run. A run over it is cancelled and the client
    gets an ``error`` event; the conversation records a failed turn.

A value that is not a positive number falls back to the default with a
warning, rather than failing the process at import or, worse, disabling the
cap.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
DEFAULT_WAIT_S = 120.0
DEFAULT_ORPHAN_GRACE_S = 60.0
DEFAULT_MAX_RUN_S = 300.0


class ChatAbandoned(RuntimeError):
    """The client gave up while its request was still waiting for a slot."""


class ChatBusyTimeout(RuntimeError):
    """No chat worker slot became free within the wait budget.

    The router maps this to HTTP 503 with a ``Retry-After`` header, before
    any stream starts, so the client sees an error instead of a hang.
    """


def _number(name: str, default: float, *, integer: bool = False) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0 or value != value or value == float("inf"):
        logger.warning(
            "Ignoring %s=%r: need a positive number; using %s", name, raw, default
        )
        return default
    return int(value) if integer else value


def max_concurrent() -> int:
    return int(_number("CHAT_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT, integer=True))


def wait_s() -> float:
    return _number("CHAT_WAIT_S", DEFAULT_WAIT_S)


def orphan_grace_s() -> float:
    return _number("CHAT_ORPHAN_GRACE_S", DEFAULT_ORPHAN_GRACE_S)


def max_run_s() -> float:
    return _number("CHAT_MAX_RUN_S", DEFAULT_MAX_RUN_S)


def _timeout(cap: int, budget: float) -> ChatBusyTimeout:
    return ChatBusyTimeout(
        f"All {cap} chat slots are busy; waited {budget:g}s. Retry shortly. "
        "Operators: raise CHAT_MAX_CONCURRENT / CHAT_WAIT_S if this keeps "
        "happening."
    )


class ChatWorkerGate:
    """A counting gate whose limit is looked up on every acquire.

    Like ``source_limits._Gate``: the limit comes from the environment, so a
    fixed-size semaphore would not do. One instance is owned by ``WarmPool``;
    each ``acquire()`` handed out to a request takes a permit, and every path
    that ends a request gives exactly one back.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_use = 0

    @property
    def in_use(self) -> int:
        with self._cond:
            return self._in_use

    def acquire(
        self, wait: float | None = None, cancel: threading.Event | None = None
    ) -> None:
        """Take a permit, blocking up to the wait budget.

        Logs when a request had to wait, for how long, and the in-use count.

        Raises:
            ChatBusyTimeout: no slot within *wait* (default ``CHAT_WAIT_S``).
            ChatAbandoned: *cancel* was set while waiting (client went away).
        """
        budget = wait_s() if wait is None else wait
        deadline = time.monotonic() + budget
        with self._cond:
            if self._in_use >= max_concurrent():
                logger.info(
                    "All %d chat worker slots are busy (%d in use); waiting up to %gs",
                    max_concurrent(),
                    self._in_use,
                    budget,
                )
                started = time.monotonic()
                while self._in_use >= max_concurrent():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _timeout(max_concurrent(), budget)
                    if cancel is not None and cancel.is_set():
                        raise ChatAbandoned("client disconnected while waiting")
                    self._cond.wait(min(remaining, 0.25) if cancel else remaining)
                logger.info(
                    "Chat worker slot granted after waiting %.1fs (in use: %d)",
                    time.monotonic() - started,
                    self._in_use,
                )
            if cancel is not None and cancel.is_set():
                raise ChatAbandoned("client disconnected while waiting")
            self._in_use += 1

    def release(self) -> None:
        with self._cond:
            self._in_use -= 1
            self._cond.notify()
