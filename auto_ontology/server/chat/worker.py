# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Long-lived prewarmed agent subprocess + a single-slot warm pool.

The agent pipeline is sync Python that spends most of its time inside
C-level calls (psycopg, httpx, embedding clients). We can't interrupt
those from another Python thread, so cancellation has to go through the
OS via process kill — same constraint as before.

What's new vs. spawn-per-request: we keep **one** subprocess alive
between requests. It imports the agent and builds the
retriever/connector singletons once, then loops on an input queue:
``("ask", question)`` → stream agent events on the output queue → idle
again. The cold-start cost is paid at server boot and at each cancel
(when we kill+respawn), not per request.

The router (see ``router.py``) now allows one in-flight stream *per
conversation* rather than one globally, so any number of conversations may
run concurrently. This pool still only keeps a single warm standby: the
first conversation to ask finds it prewarmed, and additional concurrent
conversations pay a cold start (``acquire()`` spawns a fresh subprocess on
demand).

That "no cap" behaviour was a denial of service: every concurrent
conversation was a full Python subprocess, and 18 of them at once exhausted
the host. ``acquire()`` now takes a permit from a counting gate
(:mod:`auto_ontology.server.chat.limits`) before handing out or
cold-starting a worker: at most ``CHAT_MAX_CONCURRENT`` (default 4)
conversations run at once per backend process, further requests wait up to
``CHAT_WAIT_S`` (default 120) and then fail with a clear error instead of
spawning yet another subprocess. The warm standby is extra — it never takes
a permit — so the process bound is ``CHAT_MAX_CONCURRENT + 1`` in steady
state (plus one transient replacement while a cancelled worker is being
killed and its standby successor is starting).
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from queue import Empty
from typing import Any, Generator

from auto_ontology.server.chat.limits import (
    ChatWorkerGate,
    max_concurrent,
)

logger = logging.getLogger(__name__)

# Wire-format tags. ``ready`` is sent once after imports + retriever/connector
# init complete, so the pool can tell a warming worker from a fully usable
# one (the parent doesn't strictly *need* to wait — submit() will just block
# on the worker's in_queue.get() until the loop runs — but knowing readiness
# helps with logging/health checks).
_TAG_READY = "ready"
_TAG_EVENT = "event"
_TAG_DONE = "done"
_TAG_ERROR = "error"

# Inbound message tags (parent → worker).
_MSG_ASK = "ask"

# How often events() polls the outbound queue. Doubles as the heartbeat
# cadence the router uses to write SSE comments.
_QUEUE_POLL_S = 0.25

# Grace window between SIGTERM and SIGKILL.
_TERMINATE_GRACE_S = 1.0


def _worker_loop(
    in_q: "mp.Queue[tuple[str, Any]]",
    out_q: "mp.Queue[tuple[str, Any]]",
) -> None:
    """Subprocess entry: import once, then process ``ask`` messages forever."""

    # `multiprocessing` with the `spawn` start method does NOT inherit the
    # parent's logging configuration — the child runs straight into this
    # function, never executing the parent's `logging.basicConfig` call in
    # `auto_ontology.server.__main__`. Without re-applying it here, INFO-level logs
    # from the agent ("Final answer (Xs):", per-step traces, etc.) are
    # silently dropped because Python's default root level is WARNING.
    # Subprocess stdout/stderr are inherited from the parent, so once
    # configured, log lines flow into the same terminal as everything else.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        from auto_ontology.retrieval.text_to_sql.main import (
            stream_agent_response,
        )
        from auto_ontology.retrieval.text_to_sql.follow_up import resolve_follow_up
        from auto_ontology.retrieval.text_to_sql.clarification import (
            detect_clarification as detect_clarification_fn,
        )

        from auto_ontology.connectors import get_connectors
        from auto_ontology.utils.llm_invoke import (
            get_llm_client,
            get_non_reasoning_llm_client,
        )
        from auto_ontology.utils import (
            get_data_objects_retriever,
            get_semantic_objects_retriever,
        )
        from auto_ontology.server.chat.settings_dal import (
            fetch_acronyms,
            fetch_custom_prompts,
        )

        # Eagerly build the heavyweights so they're hot before the first ask.
        data_retriever = get_data_objects_retriever()
        semantic_retriever = get_semantic_objects_retriever()
        connectors = get_connectors()
        try:
            follow_up_llm = get_non_reasoning_llm_client(max_tokens=1024)
        except Exception:
            try:
                follow_up_llm = get_llm_client(max_tokens=1024)
            except Exception:
                logger.exception(
                    "No LLM available for follow-up resolution; using raw questions"
                )
                follow_up_llm = None
    except BaseException as exc:  # noqa: BLE001 — surface init failure to parent
        logger.exception("Worker init failed")
        try:
            out_q.put((_TAG_ERROR, f"Worker init failed: {exc}"))
            out_q.put((_TAG_DONE, None))
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        out_q.put((_TAG_READY, None))
    except Exception:  # noqa: BLE001
        return

    while True:
        try:
            message = in_q.get()
        except (EOFError, OSError):
            return
        if not isinstance(message, tuple) or len(message) != 2:
            continue
        tag, payload = message
        if tag != _MSG_ASK:
            continue

        (
            question,
            prediction,
            target_db,
            subject_token,
            conversation_history,
            evidence,
            *optional,
        ) = payload
        # Appended after the original six, so a sender that omits it stays valid.
        detect_clarification = bool(optional and optional[0])
        try:
            # Connections are resolved once at worker init. If that
            # lookup came back empty — the store not yet reachable when this
            # subprocess booted, or the first connection created afterwards —
            # the snapshot would stay empty for the life of the process and
            # every question would fail with "missing required 'connectors'"
            # until the pod restarted. Re-resolve lazily so the worker heals
            # itself; get_connectors() caches a non-empty result and only
            # retries while there is nothing to cache, so this costs nothing
            # on the normal path.
            #
            # This has to run BEFORE ``ask_connectors`` is bound: binding first
            # would capture the stale empty list, and rebinding ``connectors``
            # here would not update it, leaving the healing ineffective.
            if not connectors:
                connectors = get_connectors()

            # Per-user Databricks auth trades the prewarmed connectors for ones
            # bound to the caller's exchanged token. Databricks connectors open
            # connections per operation, so rebuilding them here is cheap.
            if subject_token is not None:
                from auto_ontology.connectors.registry import (
                    get_connectors_for_subject_token,
                )

                ask_connectors = get_connectors_for_subject_token(subject_token)
            else:
                ask_connectors = connectors

            is_follow_up, processing_question = resolve_follow_up(
                question=question,
                history=conversation_history,
                llm=follow_up_llm,
            )
            logger.info(
                "Conversation context resolution: follow_up=%s processing_question=%s",
                is_follow_up,
                processing_question,
            )
            if detect_clarification:
                decision = detect_clarification_fn(
                    question=processing_question,
                    evidence=evidence or "",
                    llm=follow_up_llm,
                    semantic_retriever=semantic_retriever,
                    target_db=target_db,
                    history=conversation_history,
                )
                if decision.clarification is not None:
                    # The turn ends with the question; ``finally`` sends DONE,
                    # so the stream closes like a finished answer.
                    out_q.put((_TAG_EVENT, decision.clarification.event()))
                    continue
            custom_prompts = fetch_custom_prompts()

            agent_payload = {
                "question": question,
                "evidence": evidence or "",
                "processing_question": processing_question,
                "prediction": prediction,
                "data_retriever": data_retriever,
                "semantic_retriever": semantic_retriever,
                "connectors": ask_connectors,
                "acronyms": fetch_acronyms(),
                "custom_prompts": custom_prompts,
            }
            if target_db:
                agent_payload["target_db"] = target_db
            for event in stream_agent_response(agent_payload):
                out_q.put((_TAG_EVENT, event))
        except BaseException as exc:  # noqa: BLE001 — surface to parent
            logger.exception("Agent run failed")
            try:
                out_q.put((_TAG_ERROR, str(exc)))
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                out_q.put((_TAG_DONE, None))
            except Exception:  # noqa: BLE001
                pass


class PrewarmedWorker:
    """A subprocess running ``_worker_loop`` — imports done, ready to ask.

    Lifecycle:

        worker = PrewarmedWorker()  # spawns subprocess; returns immediately
        worker.submit(question)     # enqueue question
        for event in worker.events():  # iterate until DONE / proc death
            ...
        # Optionally reuse: worker is now idle, can take another submit().
        worker.kill()               # SIGTERM → SIGKILL; idempotent
    """

    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self._in_q: "mp.Queue[tuple[str, Any]]" = self._ctx.Queue()
        self._out_q: "mp.Queue[tuple[str, Any]]" = self._ctx.Queue()
        self._proc = self._ctx.Process(
            target=_worker_loop,
            args=(self._in_q, self._out_q),
            daemon=True,
        )
        self._proc.start()

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def submit(
        self,
        question: str,
        prediction: bool | None = None,
        target_db: str | None = None,
        subject_token: str | None = None,
        conversation_history: list[dict[str, str | None]] | None = None,
        evidence: str | None = None,
        detect_clarification: bool = False,
    ) -> None:
        """Ask *question*, optionally forcing the prediction/SQL branch.

        *prediction* mirrors the API parameter: True or False skips the
        classification step, None classifies as usual.
        *target_db* scopes the run to one connected database when set.
        *subject_token* is the caller's SSO JWT. When present the worker builds
        per-user connectors for this question instead of using the prewarmed
        ones, so SQL executes under that user's own Databricks grants.
        *evidence* is optional authoritative evidence kept separate from
        the question.
        *detect_clarification* lets the run end with a clarification question
        (a ``clarification`` event) instead of an answer.
        """
        payload = (
            question,
            prediction,
            target_db,
            subject_token,
            conversation_history or [],
            evidence,
        )
        if detect_clarification:
            # Only appended when on, so with the feature off the message is the
            # six-field one it always was.
            payload += (True,)
        self._in_q.put((_MSG_ASK, payload))

    def events(self) -> Generator[dict[str, Any] | None, None, None]:
        """Yield agent events for the current question.

        ``None`` is yielded when the queue is empty so the router can write
        an SSE heartbeat comment to keep proxies/load balancers happy.
        Returns when the worker emits ``done`` or the subprocess dies.
        Raises ``RuntimeError`` on worker-reported errors.
        """

        while True:
            try:
                tag, value = self._out_q.get(timeout=_QUEUE_POLL_S)
            except Empty:
                if not self._proc.is_alive():
                    return
                yield None
                continue
            if tag == _TAG_DONE:
                return
            if tag == _TAG_ERROR:
                raise RuntimeError(value)
            if tag == _TAG_READY:
                # Drained from a previous-readiness or warm-replay; ignore.
                continue
            yield value  # _TAG_EVENT

    def drain_until_ready(self, timeout: float | None = None) -> bool:
        """Block until the subprocess has finished its imports.

        Returns False if the worker errored or didn't become ready within
        the timeout. The pool uses this on warm spawns to log health; the
        request path doesn't need to wait — submit/events handle a still-
        warming worker transparently (they just block longer).
        """

        try:
            tag, _ = self._out_q.get(timeout=timeout)
        except Empty:
            return False
        return tag == _TAG_READY

    def kill(self) -> None:
        proc = self._proc
        if not proc.is_alive():
            return
        proc.terminate()
        proc.join(timeout=_TERMINATE_GRACE_S)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=_TERMINATE_GRACE_S)


class WarmPool:
    """Maintains exactly one prewarmed standby worker, with a capped handout.

    * ``acquire()`` — take a permit (waiting up to ``CHAT_WAIT_S``), then
      take the standby (or cold-start synchronously if it isn't there yet);
      does NOT immediately spawn a replacement, since the caller decides
      whether the acquired worker will be returned (natural end) or killed
      (cancel). Raises :class:`ChatBusyTimeout` when no permit became free.
    * ``return_alive(w)`` — request finished naturally; give the permit
      back, and if ``w`` is still alive put it back as the standby (no fresh
      spawn needed). Otherwise spawn a new standby.
    * ``replace(w)`` — request was cancelled; give the permit back, kill
      ``w`` and spawn a new standby in the background.

    Permit bookkeeping is keyed by the worker object: a worker handed out
    by ``acquire()`` is in ``_checked_out`` until it comes back through
    ``return_alive``/``replace``, which give back exactly one permit (a
    second call for the same acquisition is a no-op, so a double release
    cannot inflate capacity). The standby and its spawns never take a
    permit, so ``refresh_chat_workers()`` and ``shutdown()`` are unaffected.

    All spawn work happens off the request thread so we never block a POST
    on subprocess startup unless the standby genuinely isn't there yet.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._standby: PrewarmedWorker | None = None
        # Workers currently handed out to requests; each holds one permit.
        self._checked_out: set[PrewarmedWorker] = set()
        # True while a background standby spawn is in flight, so concurrent
        # cancels don't each start a replacement subprocess.
        self._spawning = False
        self._gate = ChatWorkerGate()
        self._spawn_async()

    def slots_in_use(self) -> int:
        """Permits currently held by in-flight requests (for health/stats)."""
        return self._gate.in_use

    def _spawn_async(self) -> None:
        with self._lock:
            # A standby exists or one is already on its way: never more
            # than one spare subprocess at a time.
            if self._standby is not None or self._spawning:
                return
            self._spawning = True
        threading.Thread(target=self._spawn_sync, daemon=True).start()

    def _spawn_sync(self) -> None:
        try:
            worker = PrewarmedWorker()
            # Block here (off the request path) until imports complete, so
            # ``acquire()`` is more likely to hand back a fully warm worker.
            # We don't enforce a timeout — initial imports can be slow on
            # cold caches and we'd rather wait than serve a half-warm one.
            worker.drain_until_ready()
            with self._lock:
                if self._standby is None:
                    self._standby = worker
                    return
            # A concurrent return_alive filled the slot first; drop the spare.
            worker.kill()
        finally:
            with self._lock:
                self._spawning = False

    def _give_back_permit(self, worker: PrewarmedWorker) -> None:
        """Release the permit held for *worker*'s acquisition, if any.

        Idempotent per acquisition: only the first ``return_alive``/
        ``replace`` for a checked-out worker releases a permit, so a double
        release cannot hand out more than the cap.
        """
        with self._lock:
            checked_out = worker in self._checked_out
            self._checked_out.discard(worker)
        if checked_out:
            self._gate.release()

    def acquire(self) -> PrewarmedWorker:
        # Permit first: nothing is handed out — warm or cold — above the cap.
        self._gate.acquire()
        try:
            with self._lock:
                w = self._standby
                self._standby = None
            if w is None or not w.is_alive():
                # Standby missing/dead — pay cold start synchronously this once.
                w = PrewarmedWorker()
                w.drain_until_ready()
        except BaseException:
            # No worker was handed out, so the permit must go straight back.
            self._gate.release()
            raise
        with self._lock:
            self._checked_out.add(w)
        return w

    def return_alive(self, worker: PrewarmedWorker) -> None:
        self._give_back_permit(worker)
        if not worker.is_alive():
            self._spawn_async()
            return
        with self._lock:
            if self._standby is None:
                self._standby = worker
                return
        worker.kill()

    def replace(self, worker: PrewarmedWorker) -> None:
        self._give_back_permit(worker)
        worker.kill()
        self._spawn_async()

    def shutdown(self) -> None:
        """Tear down on server shutdown so we don't leak orphaned subprocs."""

        with self._lock:
            standby = self._standby
            self._standby = None
        if standby is not None:
            standby.kill()


_pool: WarmPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> WarmPool:
    """Lazily-initialised module-level pool. Safe under thread races."""

    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = WarmPool()
        return _pool


def shutdown_pool() -> None:
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.shutdown()


def refresh_chat_workers() -> None:
    """Replace the warm standby worker so the next request loads fresh connectors."""
    pool = get_pool()
    with pool._lock:
        standby = pool._standby
        pool._standby = None
    if standby is not None:
        standby.kill()
    pool._spawn_async()


def chat_worker_stats() -> dict[str, int]:
    """Slots in use and the cap, for the health endpoint.

    Deliberately does not create the pool (that would spawn a subprocess):
    no chat has run yet simply means zero slots in use.
    """
    with _pool_lock:
        pool = _pool
    in_use = pool.slots_in_use() if pool is not None else 0
    return {"in_use": in_use, "cap": max_concurrent()}
