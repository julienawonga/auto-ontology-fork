# SPDX-License-Identifier: Apache-2.0

"""Refuse to measure an engine whose ontology is not in the semantic index.

With an empty index the chat falls back to the raw schema while everything still
answers, so a run measures something other than what its label says. This
happened once: a baseline was recorded against an empty index and every later
number was compared with it. The pre-flight asks the backend what it has indexed
before the first question is sent, and the report records the answer so that
``compare`` can tell later whether two reports measured the same thing.
"""

from __future__ import annotations

from typing import Any

import httpx

STATUS_PATH = "/api/semantic-compilation/status"

POPULATED = "populated"
INCOMPLETE = "incomplete"
EMPTY = "empty"


class PreflightError(Exception):
    """The run must not start; the message says why and how to override."""


def entry_state(entry: dict[str, Any]) -> str:
    """Classify one database's index entry.

    A database with nothing expected has no ontology at all, which measures the
    raw schema just as an empty index does.
    """
    expected, indexed = int(entry["expected"]), int(entry["indexed"])
    if expected == 0 or indexed == 0:
        return EMPTY
    return POPULATED if int(entry["missing"]) == 0 else INCOMPLETE


def index_state(entries: list[dict[str, Any]]) -> str:
    """One word for a set of databases: all populated, all empty, or in between."""
    states = {entry_state(e) for e in entries}
    if states == {POPULATED}:
        return POPULATED
    if states == {EMPTY}:
        return EMPTY
    return INCOMPLETE


def select_entries(
    index: list[dict[str, Any]], target_db: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """The entries a run depends on, and a problem when the target is not among them.

    With no ``target_db`` the chat may draw on every database, so all count.
    """
    if target_db is None:
        return index, None if index else "the backend reports no databases"
    chosen = [e for e in index if e["database_name"] == target_db]
    if chosen:
        return chosen, None
    names = ", ".join(sorted(e["database_name"] for e in index)) or "none"
    return [], (
        f"target database {target_db!r} is not in the semantic index status "
        f"(known: {names}); pass the database name, not its UUID"
    )


def fetch_status(client: httpx.Client, base_url: str) -> dict[str, Any]:
    try:
        response = client.get(base_url.rstrip("/") + STATUS_PATH, timeout=30.0)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PreflightError(
            f"cannot read {STATUS_PATH} from {base_url}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise PreflightError(
            f"{STATUS_PATH} returned {type(body).__name__}, not an object"
        )
    return body


def _refuse(reasons: list[str]) -> PreflightError:
    return PreflightError(
        "refusing to run: "
        + "; ".join(reasons)
        + ". A run against an empty or partial index measures the raw schema, not the "
        "ontology. Fix it (POST /api/semantic-compilation/reindex?database_name=...) "
        "or pass --allow-empty-index to measure anyway; the report will say so."
    )


def preflight(
    base_url: str,
    target_db: str | None,
    *,
    allow_empty_index: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Check the backend's index and return the provenance to put in the report meta.

    Raises ``PreflightError`` when the index is empty or incomplete for the
    target (or cannot be read) unless *allow_empty_index* is set. The backend
    URL is recorded; no documented endpoint reports a version or image id, so
    none is recorded.
    """
    meta: dict[str, Any] = {"backend_url": base_url}
    reasons: list[str] = []
    own_client = client is None
    http = client or httpx.Client()
    try:
        try:
            status = fetch_status(http, base_url)
        except PreflightError as exc:
            status = None
            reasons.append(str(exc))
            meta["ontology_index_error"] = str(exc)
    finally:
        if own_client:
            http.close()

    if status is not None:
        index = status.get("index")
        if index is None:
            # An older backend, or a comparison it could not make: either way the
            # index is unverified, which is what this check exists to prevent.
            reasons.append("the backend did not report the semantic index state")
            meta["ontology_index_error"] = "backend omitted `index`"
        else:
            chosen, problem = select_entries(index, target_db)
            meta["ontology_index"] = [
                {k: e[k] for k in ("database_name", "expected", "indexed", "missing")}
                for e in chosen
            ]
            if problem:
                reasons.append(problem)
            else:
                for entry in chosen:
                    state = entry_state(entry)
                    if state != POPULATED:
                        reasons.append(
                            f"{entry['database_name']}: index is {state} "
                            f"({entry['indexed']} of {entry['expected']} indexed, "
                            f"{entry['missing']} missing)"
                        )

    if reasons and not allow_empty_index:
        raise _refuse(reasons)
    if reasons:
        meta["allow_empty_index"] = True
        meta["ontology_index_problems"] = reasons
    return meta
