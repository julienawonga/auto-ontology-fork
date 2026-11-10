# SPDX-License-Identifier: Apache-2.0

"""Match phrases of a question to the values the database stores.

Produces the ``value_anchors`` that SQL generation already consumes (see
``prompts.format_value_anchors_section``) from the value index built by
``auto_ontology.semantic.value_index``. Without them the model filters on the
user's spelling: "USA" against a column that stores "United States", "Indai"
against "India", and the answer is a confident zero.

Three paths, cheapest and most certain first:

0. **Aliases.** A phrase a user confirmed as an alias of a stored value
   (``learn_value_alias``) wins outright, kept with the value's own row in the
   index. After the exact path (below), a built-in table of ISO 3166 country
   codes and names and US state postal codes proposes spellings ("USA", "U.S.",
   "America" for "United States"); each is used only if that exact value is
   stored. Nothing here needs an embedding call or trusts a model.
1. **Exact, after normalisation.** Both sides are casefolded, stripped of
   accents and punctuation, and a plural is tried as its singular ("comedies"
   is "Comedy", "pg 13" is "PG-13"). The index's values are held in memory, so
   this costs no embedding call.
2. **Fuzzy.** What those missed is embedded and searched among the
   indexed values, and the candidates are re-ranked by character similarity. A
   hit is kept when its embedding similarity or its character similarity
   clears a threshold. A typo ("Indai") is carried by the character ratio; an
   abbreviation ("USA" for "United States") shares almost no characters and
   rests entirely on the embedding model placing the two close. How well that
   works depends on the model, which is why the thresholds are knobs.

Everything here is best effort: a database with no index, a missing retriever
or any failure yields no anchors and never an error, because a question must
not fail for want of a hint.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable

from auto_ontology.retrieval.data_access.semantic_search import search_semantic_index
from auto_ontology.retrieval.text_to_sql.value_aliases import builtin_candidates
from auto_ontology.retrieval.text_to_sql.value_anchors import (
    split_schema_named_anchors,
)
from auto_ontology.retrieval.text_to_sql.value_text import normalize_value
from auto_ontology.semantic.constants import LABEL_COLUMN_VALUE

logger = logging.getLogger(__name__)

_LETTER = re.compile(r"[a-z]")

#: Words that are never a value on their own, and a phrase cannot begin or end
#: with one. Question words, auxiliaries, prepositions and the verbs of
#: asking; kept short on purpose, as anything left out only costs a lookup.
_STOPWORDS = frozenset(
    """
    a an the and or but not no of in on at by for from to with without into
    over under about between per as than then that this these those there
    is are was were be been being am do does did has have had having can could
    will would shall should may might must
    what which who whom whose when where why how whether
    how many much more most less least few any some all each every other
    show list give tell find get count number total sum average avg
    me my our your their its his her it they them we you i
    live lives living located based
    """.split()
)

#: Longest question phrase tried, in words.
_MAX_NGRAM = 3
#: Longest extracted entity tried as a phrase, in words.
_MAX_ENTITY_WORDS = 4
#: Embedding neighbours fetched per phrase.
_FETCH_K = 10
#: A phrase this short is too ambiguous to look up by similarity.
_MIN_FUZZY_CHARS = 3
#: Within this margin of the best hit a second column is also reported.
_SECOND_COLUMN_MARGIN = 0.05


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class ValueLinkSettings:
    """Thresholds, from the environment so they can be tuned without code."""

    #: Embedding (cosine) similarity at or above which a fuzzy hit is kept.
    min_similarity: float
    #: Character similarity (0-1) at or above which a fuzzy hit is kept.
    min_lexical: float
    #: Anchors returned per question.
    max_anchors: int
    #: Phrases sent to the embedding endpoint per question.
    max_fuzzy_phrases: int
    #: Seconds an in-memory copy of a database's values is reused.
    cache_ttl_s: float


def value_link_settings() -> ValueLinkSettings:
    return ValueLinkSettings(
        min_similarity=_float_env("VALUE_LINK_MIN_SIMILARITY", 0.80),
        min_lexical=_float_env("VALUE_LINK_MIN_LEXICAL", 0.80),
        max_anchors=max(_int_env("VALUE_LINK_MAX_ANCHORS", 5), 0),
        max_fuzzy_phrases=max(_int_env("VALUE_LINK_MAX_FUZZY_PHRASES", 6), 0),
        cache_ttl_s=_float_env("VALUE_LINK_CACHE_TTL_S", 300.0),
    )


def _singular_variants(normalized: str) -> list[str]:
    """*normalized*, then the readings of a trailing plural, most specific first."""
    variants = [normalized]
    if normalized.endswith("ies") and len(normalized) > 4:
        variants.append(normalized[:-3] + "y")
    if normalized.endswith("es") and len(normalized) > 3:
        variants.append(normalized[:-2])
    if normalized.endswith("s") and len(normalized) > 3:
        variants.append(normalized[:-1])
    return variants


# --------------------------------------------------------------------------
# The in-memory copy of a database's indexed values
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    schema_name: str | None
    table: str
    column: str
    value: str


@dataclass(frozen=True)
class _ValueIndex:
    """What one database's index holds, for in-memory lookups."""

    #: ``{normalised value: entries}``.
    values: dict[str, list[_Entry]]
    #: ``{normalised alias: entries}``, the aliases users confirmed.
    learned: dict[str, list[_Entry]]

    def __bool__(self) -> bool:
        return bool(self.values)


_cache: dict[str, tuple[float, _ValueIndex]] = {}
_cache_lock = threading.Lock()


def clear_value_cache(database_name: str | None = None) -> None:
    """Forget the cached values of *database_name*, or of every database."""
    with _cache_lock:
        if database_name is None:
            _cache.clear()
        else:
            _cache.pop(database_name, None)


def _load_values(retriever: Any, database_name: str, ttl_s: float) -> _ValueIndex:
    """The indexed values of *database_name*, from cache or one table read.

    An empty result is cached as well, so a database without an index costs one
    cheap read per ``ttl_s`` rather than one per question.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(database_name)
        if cached and now - cached[0] < ttl_s:
            return cached[1]

    rows = retriever.vdb.values_by_database(database_name, LABEL_COLUMN_VALUE)
    index = _ValueIndex({}, {})
    for row in rows:
        value = row.get("value")
        if not (value and row.get("table_name") and row.get("column_name")):
            continue
        key = normalize_value(value)
        if not key:
            continue
        entry = _Entry(
            row.get("schema_name"), row["table_name"], row["column_name"], value
        )
        index.values.setdefault(key, []).append(entry)
        for alias in row.get("aliases") or []:
            if normalize_value(alias):
                index.learned.setdefault(normalize_value(alias), []).append(entry)
    with _cache_lock:
        _cache[database_name] = (now, index)
    return index


# --------------------------------------------------------------------------
# Candidate phrases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Phrase:
    text: str
    tokens: tuple[str, ...]
    from_entity: bool
    capitalised: bool

    @property
    def normalized(self) -> str:
        return " ".join(self.tokens)


def _usable(tokens: list[str]) -> bool:
    """Whether *tokens* may be a value: some letter, no stopword at either end."""
    return (
        bool(tokens)
        and tokens[0] not in _STOPWORDS
        and tokens[-1] not in _STOPWORDS
        and bool(_LETTER.search(" ".join(tokens)))
    )


def candidate_phrases(question: str, entities: list[str]) -> list[_Phrase]:
    """Phrases of *question* that might be stored values, deduplicated.

    The extracted *entities* come first, then every 1-3 word window of the
    question. A phrase made only of digits is a number filter, not a label, and
    is left to the model, as is one that starts or ends with a stopword.
    """
    seen: set[str] = set()
    phrases: list[_Phrase] = []

    def add(
        text: str, *, from_entity: bool, capitalised: bool, after_the: bool = False
    ) -> None:
        tokens = normalize_value(text).split()
        key = " ".join(tokens)
        if key in seen or not _usable(tokens):
            return
        # "us" is also the pronoun ("which of us bought most?"). Normalising
        # case erases the sign that tells them apart, so a bare "us" found by
        # windowing the question is kept only as the written abbreviation "US"
        # or after "the" ("from the us"), which the pronoun never follows. An
        # entity the model extracted already read the sentence, so it is kept.
        if (
            tokens == ["us"]
            and not from_entity
            and text.strip() != "US"
            and not after_the
        ):
            return
        seen.add(key)
        phrases.append(_Phrase(text.strip(), tuple(tokens), from_entity, capitalised))

    for entity in entities or []:
        if isinstance(entity, str) and len(entity.split()) <= _MAX_ENTITY_WORDS:
            add(entity, from_entity=True, capitalised=False)

    words = re.findall(r"[\w'-]+", question or "")
    for size in range(_MAX_NGRAM, 0, -1):
        for start in range(len(words) - size + 1):
            window = words[start : start + size]
            # A capital or an upper-case run is how a name or an abbreviation
            # shows up in a question; it is the best hint of a value.
            capitalised = start > 0 and any(w[:1].isupper() for w in window)
            after_the = start > 0 and words[start - 1].lower() == "the"
            add(
                " ".join(window),
                from_entity=False,
                capitalised=capitalised,
                after_the=after_the,
            )
    return phrases


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _anchor(phrase: str, entry: _Entry) -> dict[str, str]:
    """An anchor in the shape ``format_value_anchors_section`` reads."""
    return {
        "phrase": phrase,
        "kind": "value",
        "tbl": entry.table,
        "col": entry.column,
        "stored_value": entry.value,
    }


def _exact(phrase: _Phrase, index: _ValueIndex) -> list[_Entry]:
    for variant in _singular_variants(phrase.normalized):
        if variant in index.values:
            return sorted(index.values[variant], key=lambda e: (e.table, e.column))[:2]
    return []


def _by_alias(phrase: _Phrase, index: _ValueIndex) -> list[_Entry]:
    """Readings of *phrase* from the built-in aliases.

    A built-in alias only proposes spellings; each becomes an entry only if that
    exact value is stored in an indexed column, so no value is ever invented.
    An alias that stands for several entities ("CA") reports one entry per
    entity that is stored. When the database holds only one of them, the other
    is not a reading here, so it is the only candidate; when it holds several
    they are all returned, never one chosen silently.
    """
    entries: list[_Entry] = []
    for names in builtin_candidates(phrase.normalized):
        for name in names:
            stored = index.values.get(normalize_value(name))
            if stored:
                entries.append(sorted(stored, key=lambda e: (e.table, e.column))[0])
                break
    return entries[:3]


def _deterministic(phrase: _Phrase, index: _ValueIndex) -> list[_Entry]:
    """Learned alias, then the value itself, then built-in alias."""
    learned = index.learned.get(phrase.normalized)
    if learned:
        return sorted(learned, key=lambda e: (e.table, e.column))[:2]
    return _exact(phrase, index) or _by_alias(phrase, index)


@dataclass(frozen=True)
class _Scored:
    entry: _Entry
    score: float


def _fuzzy(
    phrase: _Phrase,
    retriever: Any,
    database_name: str,
    settings: ValueLinkSettings,
    search: Callable[..., list[dict]],
) -> list[_Scored]:
    hits = search(
        retriever,
        phrase.text,
        label_filter=[LABEL_COLUMN_VALUE],
        per_label_k=_FETCH_K,
        database_name=database_name,
    )
    best_per_column: dict[tuple[str, str], _Scored] = {}
    for hit in hits:
        value = hit.get("text") or hit.get("name")
        table, column = hit.get("table_name"), hit.get("source_column")
        # The search is already filtered to this database; a row that names
        # another one anyway is never used.
        if not (value and table and column) or hit.get("database_name") not in (
            None,
            database_name,
        ):
            continue
        # The score is a cosine distance; the rest of the range is similarity.
        similarity = 1.0 - float(hit.get("score", 1.0))
        lexical = SequenceMatcher(
            None, phrase.normalized, normalize_value(value)
        ).ratio()
        if similarity < settings.min_similarity and lexical < settings.min_lexical:
            continue
        scored = _Scored(
            _Entry(hit.get("schema_name"), table, column, value),
            max(similarity, lexical),
        )
        key = (table, column)
        if key not in best_per_column or scored.score > best_per_column[key].score:
            best_per_column[key] = scored
    ranked = sorted(best_per_column.values(), key=lambda s: -s.score)
    if not ranked:
        return []
    # One reading per column; a second column only when it is nearly as good,
    # which is a real ambiguity rather than a weaker guess.
    return [ranked[0]] + [
        s for s in ranked[1:2] if s.score >= ranked[0].score - _SECOND_COLUMN_MARGIN
    ]


def link_question_values(
    question: str,
    entities: list[str] | None,
    database_name: str | None,
    retriever: Any,
    *,
    relevant_tables: list[dict] | None = None,
    search: Callable[..., list[dict]] = search_semantic_index,
) -> list[dict[str, str]]:
    """Anchors for the phrases of *question* that name a stored value.

    Returns ``[]`` without any embedding call when *database_name* has no value
    index, and on any failure. Only *database_name*'s index is read or searched.
    ``relevant_tables``, when given, spares lookups for phrases that name those
    tables or their columns, which are not values.
    """
    if not database_name or retriever is None:
        return []
    try:
        settings = value_link_settings()
        if settings.max_anchors == 0:
            return []
        index = _load_values(retriever, database_name, settings.cache_ttl_s)
        if not index:
            return []

        phrases = candidate_phrases(question, entities or [])
        if relevant_tables:
            kept, _ = split_schema_named_anchors(
                [{"phrase": p.text} for p in phrases], relevant_tables
            )
            kept_texts = {a["phrase"] for a in kept}
            phrases = [p for p in phrases if p.text in kept_texts]

        anchors: list[dict[str, str]] = []
        consumed: set[str] = set()

        def room() -> bool:
            return len(anchors) < settings.max_anchors

        def free(phrase: _Phrase) -> bool:
            return consumed.isdisjoint(phrase.tokens)

        # Longest first, so "united states" is taken whole and "states" is not
        # then reported as a value of its own.
        for phrase in sorted(phrases, key=lambda p: -len(p.tokens)):
            if not room():
                break
            if not free(phrase):
                continue
            entries = _deterministic(phrase, index)
            if entries:
                anchors.extend(_anchor(phrase.text, e) for e in entries)
                consumed.update(phrase.tokens)

        fuzzy = [
            p for p in phrases if free(p) and len(p.normalized) >= _MIN_FUZZY_CHARS
        ]
        # Entities, then names and abbreviations, then longer phrases first.
        fuzzy.sort(key=lambda p: (not p.from_entity, not p.capitalised, -len(p.tokens)))
        batch = fuzzy[: settings.max_fuzzy_phrases]
        if batch and room():
            # One embedding call per phrase, so they run side by side: a
            # question must not wait for them one after another.
            with ThreadPoolExecutor(max_workers=min(len(batch), 4)) as pool:
                futures = [
                    pool.submit(_fuzzy, p, retriever, database_name, settings, search)
                    for p in batch
                ]
            for phrase, future in zip(batch, futures):
                try:
                    matches = future.result()
                except Exception:
                    # The exact matches stand; a failed lookup only costs its own
                    # phrase.
                    logger.warning(
                        "Value linking: embedding lookup failed for %r",
                        phrase.text,
                        exc_info=True,
                    )
                    continue
                if matches and room() and free(phrase):
                    anchors.extend(_anchor(phrase.text, m.entry) for m in matches)
                    consumed.update(phrase.tokens)
        return anchors[: settings.max_anchors]
    except Exception:
        # A hint that cannot be produced is no reason to fail the question.
        logger.warning(
            "Value linking failed; continuing without anchors", exc_info=True
        )
        return []


def learn_value_alias(
    retriever: Any,
    database_name: str,
    *,
    schema_name: str | None,
    table: str,
    column: str,
    value: str,
    alias: str,
) -> bool:
    """Record that *alias* means the stored *value* of ``table.column``.

    The hook a later clarification step will call once a user confirms what a
    phrase meant; nothing in the chat calls it yet. The alias is kept in the
    metadata of the value's own row in the index, so it is scoped to its
    database, table and column by construction, is consulted before the built-in
    aliases, and survives a rebuild of the index. Returns False when the value
    is not indexed (an alias is never stored for a value the index does not hold).
    """
    if not normalize_value(alias):
        return False
    done = retriever.vdb.add_value_alias(
        database_name,
        schema_name=schema_name,
        table_name=table,
        column_name=column,
        value=value,
        alias=alias.strip(),
    )
    clear_value_cache(database_name)
    return bool(done)
