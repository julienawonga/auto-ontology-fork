# SPDX-License-Identifier: Apache-2.0

"""Experiment E, shared part: split a Markdown wiki into chunks and rank them.

The wiki is a Markdown file per database that lives outside the repository.
``chunk_markdown`` is deterministic (same text, same chunks, same ids) so the
two arms and a later re-run see identical chunks:

* E-rag: ``rag_cases`` writes a NEW cases file whose ``evidence`` is the top-k
  chunks for each question (arm B's channel, fed by retrieval instead of by the
  benchmark's own knowledge entries).
* E-auto: ``wiki_extract`` compiles the same chunks into Terms.

Nothing here reads a gold answer; a case's gold is copied through untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

#: A section longer than this is split on paragraph boundaries.
DEFAULT_MAX_CHARS = 3000
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to "
    "was were what which who with".split()
)


@dataclass(frozen=True)
class Chunk:
    id: str
    path: tuple[str, ...]  # heading path, outermost first
    text: str  # the body, without its heading line

    @property
    def heading(self) -> str:
        return " > ".join(self.path)

    def render(self) -> str:
        """The chunk as evidence text: heading path, then the body."""
        return f"[{self.heading}]\n{self.text}" if self.path else self.text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "heading_path": list(self.path), "text": self.text}


def slug(text: str) -> str:
    return "-".join(_TOKEN.findall(text.lower())) or "section"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sections(markdown: str) -> list[tuple[tuple[str, ...], str]]:
    """(heading path, body) for every heading that has a body, in file order."""
    stack: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    body: list[str] = []
    out: list[tuple[tuple[str, ...], str]] = []
    in_fence = False
    in_math = False

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            out.append((path, text))
        body.clear()

    for line in markdown.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif line.count("$$") % 2 == 1:  # a $$ display-math block opens or closes
            in_math = not in_math
        match = None if (in_fence or in_math) else _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            path = tuple(title for _, title in stack)
        else:
            body.append(line)
    flush()
    return out


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs; a fenced or ``$$`` block never splits."""
    paras: list[str] = []
    current: list[str] = []
    in_fence = in_math = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif line.count("$$") % 2 == 1:
            in_math = not in_math
        if not line.strip() and not (in_fence or in_math):
            if current:
                paras.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paras.append("\n".join(current))
    return paras


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split on blank lines into parts of at most *max_chars* (a single longer
    paragraph or math block stays whole: text is never cut mid-formula)."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for para in _paragraphs(text):
        if current and len(current) + len(para) + 2 > max_chars:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def chunk_markdown(markdown: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Section chunks of *markdown*, with ids stable across runs.

    The id is ``<slug of the heading path>-<6 hex of its hash>``, ``-N`` when the
    same path occurs again, and ``.pN`` for the parts of a long section. Text
    before the first heading is a chunk with an empty path (id ``preamble``).
    """
    chunks: list[Chunk] = []
    seen: Counter[tuple[str, ...]] = Counter()
    for path, body in _sections(markdown):
        seen[path] += 1
        digest = hashlib.sha1(" > ".join(path).encode("utf-8")).hexdigest()[:6]
        base = f"{slug(' '.join(path))}-{digest}" if path else "preamble"
        if seen[path] > 1:
            base += f"-{seen[path]}"
        parts = _split_long(body, max_chars)
        for i, part in enumerate(parts, 1):
            cid = base if len(parts) == 1 else f"{base}.p{i}"
            chunks.append(Chunk(cid, path, part))
    return chunks


def load_wiki(
    path: Path, *, max_chars: int = DEFAULT_MAX_CHARS
) -> tuple[list[Chunk], str]:
    """(chunks, sha256 of the file's text) of a wiki file."""
    text = path.read_text(encoding="utf-8")
    return chunk_markdown(text, max_chars=max_chars), sha256_text(text)


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class Similarity(Protocol):
    """Anything that can rank chunks for a question."""

    name: str

    def rank(
        self, question: str, chunks: Sequence[Chunk], k: int
    ) -> list[tuple[Chunk, float]]: ...


def _top_k(
    chunks: Sequence[Chunk], scores: Sequence[float], k: int
) -> list[tuple[Chunk, float]]:
    # Ties keep file order, so a ranking never depends on hash order.
    order = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))
    return [(chunks[i], float(scores[i])) for i in order[: max(0, k)]]


class Bm25:
    """Okapi BM25 over chunk text and heading path, pure Python."""

    name = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._cache_key: tuple[str, ...] | None = None
        self._docs: list[Counter[str]] = []
        self._lens: list[int] = []
        self._avg = 0.0
        self._idf: dict[str, float] = {}

    def _index(self, chunks: Sequence[Chunk]) -> None:
        key = tuple(c.id for c in chunks)
        if key == self._cache_key:
            return
        self._docs = [Counter(tokenize(f"{c.heading} {c.text}")) for c in chunks]
        self._lens = [sum(d.values()) for d in self._docs]
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 0.0
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(d.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self._cache_key = key

    def scores(self, question: str, chunks: Sequence[Chunk]) -> list[float]:
        self._index(chunks)
        terms = [t for t in set(tokenize(question)) if t in self._idf]
        out = []
        for doc, length in zip(self._docs, self._lens):
            norm = self.k1 * (1 - self.b + self.b * length / (self._avg or 1.0))
            out.append(
                sum(
                    self._idf[t] * doc[t] * (self.k1 + 1) / (doc[t] + norm)
                    for t in terms
                    if t in doc
                )
            )
        return out

    def rank(
        self, question: str, chunks: Sequence[Chunk], k: int
    ) -> list[tuple[Chunk, float]]:
        scores = self.scores(question, chunks)
        return [(c, s) for c, s in _top_k(chunks, scores, k) if s > 0]


#: ``embed(texts, kind)`` -> one vector per text; kind is "query" or "passage".
EmbedFn = Callable[[list[str], str], list[list[float] | None]]


class EmbeddingSimilarity:
    """Cosine similarity over vectors from an injected embedding function."""

    name = "embedding"

    def __init__(self, embed: EmbedFn) -> None:
        self._embed = embed
        self._key: tuple[str, ...] | None = None
        self._vecs: list[list[float] | None] = []

    def rank(
        self, question: str, chunks: Sequence[Chunk], k: int
    ) -> list[tuple[Chunk, float]]:
        key = tuple(c.id for c in chunks)
        if key != self._key:
            self._vecs = self._embed([c.render() for c in chunks], "passage")
            self._key = key
        query = self._embed([question], "query")[0]
        if query is None:
            return []
        qn = math.sqrt(sum(x * x for x in query)) or 1.0
        scores = []
        for vec in self._vecs:
            if vec is None:
                scores.append(0.0)
                continue
            vn = math.sqrt(sum(x * x for x in vec)) or 1.0
            scores.append(sum(a * b for a, b in zip(query, vec)) / (qn * vn))
        return _top_k(chunks, scores, k)


def repo_embedding_similarity() -> EmbeddingSimilarity:
    """The repo's own embedding client (``EMBED_*`` settings), imported lazily."""
    from auto_ontology.utils.embedding import embed_texts, get_embed_params

    params = get_embed_params()

    def embed(texts: list[str], kind: str) -> list[list[float] | None]:
        return embed_texts(texts, params, "query" if kind == "query" else "passage")

    return EmbeddingSimilarity(embed)


# --------------------------------------------------------------------------- #
# E-rag
# --------------------------------------------------------------------------- #


def build_evidence(
    hits: Sequence[tuple[Chunk, float]], *, max_chars: int | None = None
) -> str:
    """The chunks as one evidence string, best first; cut at *max_chars* when set
    (a chunk that would overflow the budget is dropped, never truncated)."""
    parts: list[str] = []
    used = 0
    for chunk, _ in hits:
        text = chunk.render()
        extra = len(text) + (2 if parts else 0)
        if max_chars is not None and used + extra > max_chars:
            break
        parts.append(text)
        used += extra
    return "\n\n".join(parts)


def _cases_list(payload: Any) -> list[dict[str, Any]]:
    cases = payload["cases"] if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("cases file: expected a list or an object with 'cases'")
    return cases


def rag_cases(
    payload: Any,
    chunks: Sequence[Chunk],
    sim: Similarity,
    *,
    k: int,
    max_chars: int | None = None,
    wiki_sha256: str = "",
) -> tuple[Any, dict[str, Any]]:
    """A copy of a cases payload whose ``evidence`` is the top-k chunks.

    Returns (new payload, summary). The input is not modified, every other case
    field (gold included) is copied as is, and the payload records the arm's
    parameters so a report can say which k / budget it used.
    """
    new = json.loads(json.dumps(payload))
    chosen: dict[str, list[str]] = {}
    replaced = 0
    for case in _cases_list(new):
        hits = sim.rank(str(case["question"]), chunks, k)
        if case.get("evidence"):
            replaced += 1
        case["evidence"] = build_evidence(hits, max_chars=max_chars)
        chosen[str(case["id"])] = [c.id for c, _ in hits]
    meta = {
        "arm": "E-rag",
        "similarity": sim.name,
        "k": k,
        "max_chars": max_chars,
        "wiki_sha256": wiki_sha256,
        "chunks": len(chunks),
    }
    if isinstance(new, dict):
        new["wiki_rag"] = meta
    summary = {**meta, "cases": len(chosen), "evidence_replaced": replaced}
    return new, summary
