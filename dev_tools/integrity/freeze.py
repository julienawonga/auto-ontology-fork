# SPDX-License-Identifier: Apache-2.0

"""Freeze an eval case file so that editing it afterwards is a visible act.

A case file may declare ``frozen_at`` and ``frozen_sha256``. The hash covers the
``cases`` only (questions, gold SQL, expected rows, flags), computed over a
canonical JSON form so reformatting the file does not change it. Editing a case
after the fact then fails the integrity check until someone re-freezes the file
on purpose, which puts the edit in the diff of a pull request where it can be
questioned. The eval harness records the same hash in every report, so reports
measured on different versions of a cases file cannot be compared by accident.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


def cases_sha256(cases: list[Any]) -> str:
    canonical = json.dumps(
        cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_cases_sha256(path: Path) -> str:
    """Hash of the ``cases`` of a case file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return cases_sha256(raw["cases"])


def freeze(path: Path, *, today: dt.date | None = None) -> str:
    """Write ``frozen_at`` and ``frozen_sha256`` into *path*; return the hash.

    Running it again on a frozen file overwrites both: that is the deliberate
    way to accept a change, and it shows up in the diff.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    digest = cases_sha256(raw["cases"])
    stamp = (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()
    # Keep the markers next to the file's metadata, ahead of the long `cases`.
    head = {
        k: v for k, v in raw.items() if k not in ("cases", "frozen_at", "frozen_sha256")
    }
    out = {**head, "frozen_at": stamp, "frozen_sha256": digest, "cases": raw["cases"]}
    # Same serialisation as `snapshot`, so freezing changes only the two markers.
    # Bytes, not write_text: on Windows text mode would turn every line into CRLF.
    path.write_bytes((json.dumps(out, indent=2) + "\n").encode("utf-8"))
    return digest


def check_frozen(path: Path) -> str | None:
    """Return a problem message, or ``None`` when the file is unfrozen or intact."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    has_at, has_hash = "frozen_at" in raw, "frozen_sha256" in raw
    if not has_at and not has_hash:
        return None
    if has_at != has_hash:
        return f"{path.name}: declares only one of frozen_at and frozen_sha256"
    try:
        dt.date.fromisoformat(str(raw["frozen_at"]))
    except ValueError:
        return f"{path.name}: frozen_at {raw['frozen_at']!r} is not an ISO date"
    actual = cases_sha256(raw["cases"])
    if actual != raw["frozen_sha256"]:
        return (
            f"{path.name}: frozen on {raw['frozen_at']} but its cases changed "
            f"(recorded {str(raw['frozen_sha256'])[:12]}, now {actual[:12]}). If the "
            f"change is deliberate, say why in the pull request and run "
            f"`python -m dev_tools.integrity freeze {path.name}`."
        )
    return None
