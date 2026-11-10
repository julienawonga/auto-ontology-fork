# SPDX-License-Identifier: Apache-2.0

"""How phrases and stored values are compared: one normalisation for both."""

from __future__ import annotations

import re
import unicodedata

_TOKEN = re.compile(r"[a-z0-9]+")


def normalize_value(text: str) -> str:
    """Casefolded, accent-free, punctuation-free words of *text*, space-joined."""
    folded = unicodedata.normalize("NFKD", str(text)).casefold()
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(_TOKEN.findall(stripped))
