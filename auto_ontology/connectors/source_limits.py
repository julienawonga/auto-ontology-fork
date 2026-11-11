# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounds on the connections this process opens to *source* databases.

A source database is one a user registered under Settings → Connections; it is
not the Lexis store (see :mod:`auto_ontology.dal.session` for that pool).
Registering N sources used to cost N pools held open from startup, in the backend
*and* in the ingestion service, so a stack with a few dozen sources could exhaust
the sources' own ``max_connections`` while doing nothing.

Three knobs, all read at call time (so a test or an operator can change them
without rebuilding connectors):

``SOURCE_POOL_MAX_SIZE`` (default 2)
    Connections one source's pool may open. This is also the number of queries
    one source runs at once: a request beyond it waits, it does not fail.

``SOURCE_POOL_MAX_IDLE_S`` (default 30)
    How long an unused connection stays open before the pool closes it. A pool
    starts with *no* connections and never keeps a minimum.

``SOURCE_MAX_CONNECTIONS`` (default 12)
    Cap on connections checked out to run a statement, summed over every source,
    **per process**. The backend, the ingestion service and each spawned chat
    worker are separate processes and each enforces its own cap, so budget
    ``(workers + 2) x SOURCE_MAX_CONNECTIONS`` against a source's
    ``max_connections`` (see ``docs/source-connections.md``).

``SOURCE_CONNECTION_WAIT_S`` (default 60)
    How long a request waits for a slot before giving up with
    :class:`SourceConnectionTimeout`. Waiting is the point: under a burst the
    cap should queue work, not turn it into errors.

A value that is not a positive number falls back to the default with a warning,
rather than failing the process at import or, worse, disabling the cap.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DEFAULT_POOL_MAX_SIZE = 2
DEFAULT_POOL_MAX_IDLE_S = 30.0
DEFAULT_MAX_CONNECTIONS = 12
DEFAULT_WAIT_S = 60.0


class SourceConnectionTimeout(RuntimeError):
    """No source-database connection became free within the wait budget.

    Deliberately not a :class:`~auto_ontology.connectors.base.StatementTimeout`:
    the statement never started, so "simplify the query" would be the wrong
    advice, and the sampling circuit breaker must not count it as a slow query.
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


def pool_max_size() -> int:
    return int(_number("SOURCE_POOL_MAX_SIZE", DEFAULT_POOL_MAX_SIZE, integer=True))


def pool_max_idle_s() -> float:
    return _number("SOURCE_POOL_MAX_IDLE_S", DEFAULT_POOL_MAX_IDLE_S)


def max_connections() -> int:
    return int(_number("SOURCE_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS, integer=True))


def wait_s() -> float:
    return _number("SOURCE_CONNECTION_WAIT_S", DEFAULT_WAIT_S)


class _Gate:
    """A counting gate whose limit is looked up on every acquire.

    ``threading.BoundedSemaphore`` fixes its size at construction; the limit
    here comes from the environment, so it has to be re-read.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_use = 0

    @property
    def in_use(self) -> int:
        with self._cond:
            return self._in_use

    def acquire(self, deadline: float) -> bool:
        with self._cond:
            while self._in_use >= max_connections():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._in_use += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._in_use -= 1
            self._cond.notify()


_global_gate = _Gate()


def connections_in_use() -> int:
    """Statements currently holding a source connection in this process."""
    return _global_gate.in_use


class SourceLimiter:
    """Per-source slots, handed out before the process-wide ones.

    One limiter per source sized to its pool, so the pool itself never has to
    make a request wait: waiting happens here, *before* taking a process-wide
    slot. Taking the global slot first would let one busy source fill the global
    slots with requests that cannot run yet, starving every other source.
    """

    def __init__(self, size: int) -> None:
        self._sem = threading.BoundedSemaphore(size)

    @contextmanager
    def slot(self, source: str, wait: float | None = None) -> Iterator[None]:
        """Hold a slot for one statement.

        Raises:
            SourceConnectionTimeout: no slot within *wait* (default
                ``SOURCE_CONNECTION_WAIT_S``).
        """
        budget = wait_s() if wait is None else wait
        deadline = time.monotonic() + budget
        if not self._sem.acquire(timeout=max(budget, 0)):
            raise _timeout(source, budget, "this source's own connection limit")
        try:
            if not _global_gate.acquire(deadline):
                raise _timeout(source, budget, "the process-wide connection limit")
            try:
                yield
            finally:
                _global_gate.release()
        finally:
            self._sem.release()


def _timeout(source: str, budget: float, which: str) -> SourceConnectionTimeout:
    return SourceConnectionTimeout(
        f"Waited {budget:g}s for a connection to source database {source!r} and "
        f"none became free ({which}). The database is busy with other requests; "
        "retry shortly. Operators: raise SOURCE_CONNECTION_WAIT_S, "
        "SOURCE_MAX_CONNECTIONS or SOURCE_POOL_MAX_SIZE if this keeps happening."
    )


@contextmanager
def global_slot(source: str, wait: float | None = None) -> Iterator[None]:
    """A process-wide slot for a connection that bypasses a pool (``ping``)."""
    budget = wait_s() if wait is None else wait
    if not _global_gate.acquire(time.monotonic() + budget):
        raise _timeout(source, budget, "the process-wide connection limit")
    try:
        yield
    finally:
        _global_gate.release()
