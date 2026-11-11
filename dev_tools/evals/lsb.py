# SPDX-License-Identifier: Apache-2.0

"""LiveSQLBench-Base-Lite: build case files, and score with the benchmark's rule.

The questions are public; the gold SQL and the knowledge-entry ids come from the
BIRD team under a **no-redistribution** request. Nothing here reads them from, or
writes them to, the repository: every function takes paths, and ``lsb-build``
refuses an output directory inside the checkout.

``build_cases`` turns the SELECT ("Query") tasks into one case file per database.
There are two scoring modes (``lsb-score --mode``):

``official`` (the default) transcribes the benchmark's SELECT metric as it is
written, quirks included: a plain-regex rewrite that also strips
``COUNT(DISTINCT x)`` and breaks ``IS DISTINCT FROM`` (the official code does
both), ``ROUND(x)`` and ``ROUND(x, n)`` replaced by the bare first argument with
no added parentheses, whitespace collapsed to single spaces even inside string
literals, at most 10,000 rows read, and numbers rounded to 2 decimals
ROUND_HALF_UP before comparison, including numbers nested in arrays and JSON.

``clean`` is our own, more careful reading of the same published rule: only the
``SELECT DISTINCT`` modifier is stripped (``DISTINCT ON``, ``COUNT(DISTINCT x)``
and ``IS DISTINCT FROM`` keep their meaning), ``ROUND(x, n)`` becomes ``(x)``
so precedence is kept, comments are removed by a quote-aware tokenizer, string
literals are never touched, and non-finite numbers compare as text instead of
erroring. `docs/evaluation.md` lists every difference.

Both modes run the rewritten queries in a read-only, statement-timeout-bounded
Postgres session, turn dates into ``YYYY-MM-DD``, compare the rows as sets, or
as lists when the task requires an order, and score 0 for an error or an empty
result on either side.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Context, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from dev_tools.evals.cases import Case
from dev_tools.evals.report import Report

SOURCE = "livesqlbench-base-lite"
TIERS = ("Simple", "Moderate", "Challenging")
TIER_TAG = "tier:"
# Frozen reports use plain lowercase tier tags; lsb-build writes "tier:Simple".
PLAIN_TIERS = {tier.lower(): tier for tier in TIERS}
GOLD_LABEL = "gold"
DEFAULT_STATEMENT_TIMEOUT_S = 60.0

# Scoring modes: "official" transcribes the benchmark's own metric (default),
# "clean" is our more careful reading of the same published rule.
OFFICIAL = "official"
CLEAN = "clean"
MODES = (OFFICIAL, CLEAN)
# The benchmark's own harness reads at most 10,000 rows per query.
OFFICIAL_FETCH_CAP = 10_000
# Report ids (and lsb-build case ids) may carry an "lsb." prefix the other
# side lacks; matching ignores it.
ID_PREFIX = "lsb."

# ---------------------------------------------------------------- SQL rewriting

Token = tuple[str, str]  # (kind, text); kind: comment string word ws other

_TOKEN_RE = re.compile(
    r"""
      (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^']|'')*'|"(?:[^"]|"")*"|\$(?P<tag>\w*)\$.*?\$(?P=tag)\$)
    | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<ws>\s+)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def tokenize(sql: str) -> list[Token]:
    """Split SQL so that strings and comments are never mistaken for code."""
    return [(m.lastgroup or "other", m.group()) for m in _TOKEN_RE.finditer(sql)]


def _join(tokens: Iterable[Token]) -> str:
    return "".join(text for _, text in tokens)


def _significant(tokens: Sequence[Token], start: int, step: int) -> int | None:
    """Index of the nearest token from *start* going by *step* that is not blank."""
    i = start
    while 0 <= i < len(tokens):
        if tokens[i][0] not in ("ws", "comment"):
            return i
        i += step
    return None


def strip_comments(sql: str) -> str:
    return _join(
        (" ", " ") if kind == "comment" else (kind, text)
        for kind, text in tokenize(sql)
    )


def strip_distinct(sql: str) -> str:
    """Drop the ``SELECT DISTINCT`` modifier; ``DISTINCT ON`` stays.

    ``COUNT(DISTINCT x)`` and ``IS DISTINCT FROM`` are left alone: removing them
    changes what the query computes, not just its row set. The official metric
    strips them anyway; ``lsb-score --mode official`` does, ``--mode clean``
    uses this function.
    """
    tokens = tokenize(sql)
    for i, (kind, text) in enumerate(tokens):
        if kind != "word" or text.upper() != "DISTINCT":
            continue
        prev, nxt = _significant(tokens, i - 1, -1), _significant(tokens, i + 1, 1)
        before = tokens[prev][1].upper() if prev is not None else ""
        after = tokens[nxt][1].upper() if nxt is not None else ""
        if after == "ON":
            continue
        if before == "SELECT":
            tokens[i] = ("ws", "")
    return _join(tokens)


def assert_single_statement(sql: str) -> None:
    """Raise ``ValueError`` unless *sql* is exactly one SQL statement.

    psycopg runs multi-statement strings in one ``execute`` call, so
    ``SELECT 1; COMMIT; <anything>`` would escape a read-only transaction.
    A semicolon inside a string literal, a quoted identifier, a dollar-quoted
    block or a comment is not a separator: the tokenizer knows those. A single
    trailing semicolon is fine.
    """
    seen_separator = False
    for kind, text in tokenize(sql):
        if (kind, text) == ("other", ";"):
            seen_separator = True
        elif kind not in ("ws", "comment") and seen_separator:
            raise ValueError(
                "multiple statements in one query are not allowed: " + sql[:120]
            )


def _matching_paren(tokens: Sequence[Token], open_at: int) -> int | None:
    depth = 0
    for i in range(open_at, len(tokens)):
        if tokens[i][0] != "other":
            continue
        if tokens[i][1] == "(":
            depth += 1
        elif tokens[i][1] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _split_args(tokens: Sequence[Token]) -> list[list[Token]]:
    args: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token[0] == "other" and token[1] in "([":
            depth += 1
        elif token[0] == "other" and token[1] in ")]":
            depth -= 1
        if token == ("other", ",") and depth == 0:
            args.append([])
        else:
            args[-1].append(token)
    return args


def _strip_round(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        if kind == "word" and text.upper() == "ROUND":
            j = _significant(tokens, i + 1, 1)
            if j is not None and tokens[j] == ("other", "("):
                end = _matching_paren(tokens, j)
                if end is not None:
                    args = _split_args(tokens[j + 1 : end])
                    if len(args) == 2:
                        # Parenthesised, so ``a * ROUND(b + c, 2)`` keeps its meaning.
                        out += [("other", "("), *_strip_round(args[0]), ("other", ")")]
                        i = end + 1
                        continue
        out.append(tokens[i])
        i += 1
    return out


def strip_round(sql: str) -> str:
    """Replace ``ROUND(x, n)`` by ``(x)``, at any nesting. ``ROUND(x)`` stays."""
    return _join(_strip_round(tokenize(sql)))


# ------------------------------------------------------- the official rewrites
# A transcription of the benchmark's own SELECT-metric rewrite (its logic, our
# code; quirks reproduced on purpose so ``--mode official`` scores exactly as
# the official harness does).

_OFFICIAL_DISTINCT_RE = re.compile(r"\bDISTINCT\b(?![^()]*\bON\b)", re.IGNORECASE)
_OFFICIAL_ROUND_RE = re.compile(r"ROUND\s*\(", re.IGNORECASE)


def official_strip_comments(sql: str) -> str:
    """Plain-regex comment removal, exactly as the official code does it.

    Not quote-aware: a ``--`` inside a string literal still starts a "comment",
    and a ``--`` comment with no newline after it is **not** removed.
    """
    s = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    s = re.sub(r"--.*?(\r\n|\r|\n)", r"\1", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def official_strip_distinct(sql: str) -> str:
    """Remove every ``DISTINCT`` that is not (soon) followed by ``ON``.

    ``COUNT(DISTINCT x)`` loses its ``DISTINCT`` and ``a IS DISTINCT FROM b``
    becomes a syntax error -- exactly like the official code. All whitespace,
    including inside string literals, collapses to single spaces.
    """
    q = _OFFICIAL_DISTINCT_RE.sub("", sql)
    return re.sub(r"\s+", " ", q).strip()


def _official_matching_paren(s: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _official_first_arg_end(s: str, start: int) -> int:
    """End of the first argument: the first top-level comma, else the paren."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            if depth == 0:
                return i
            depth -= 1
        elif s[i] == "," and depth == 0:
            return i
    return len(s)


def official_strip_round(s: str) -> str:
    """Replace ``ROUND(x, n)`` **and** ``ROUND(x)`` by the bare ``x``.

    No parentheses are added, so ``2 * ROUND(a + b, 2)`` becomes
    ``2 * a + b`` and changes meaning -- as in the official code. Unbalanced
    parentheses leave the rest of the string alone.
    """
    while True:
        m = _OFFICIAL_ROUND_RE.search(s)
        if m is None:
            return s
        op = m.end() - 1
        cp = _official_matching_paren(s, op)
        if cp == -1:
            return s
        e1 = _official_first_arg_end(s, op + 1)
        s = s[: m.start()] + s[op + 1 : e1].strip() + s[cp + 1 :]


def official_clean(sql: str) -> str:
    """The query as the benchmark's own metric runs it."""
    return official_strip_round(official_strip_distinct(official_strip_comments(sql)))


def normalize_sql(sql: str, *, mode: str = OFFICIAL) -> str:
    """The query as the chosen metric runs it (``official`` by default)."""
    if mode == OFFICIAL:
        return official_clean(sql)
    if mode == CLEAN:
        out = strip_round(strip_distinct(strip_comments(sql)))
        return out.strip().rstrip(";").strip()
    raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


def repair_trailing_comma(sql: str) -> str:
    """Remove a comma that sits directly before ``FROM``.

    No valid PostgreSQL has one, so removing it cannot change a working query.
    One official gold query (``archeology_7``) has this typo.
    """
    tokens = tokenize(sql)
    for i, token in enumerate(tokens):
        if token == ("other", ","):
            nxt = _significant(tokens, i + 1, 1)
            if (
                nxt is not None
                and tokens[nxt][0] == "word"
                and tokens[nxt][1].upper() == "FROM"
            ):
                tokens[i] = ("ws", "")
    return _join(tokens)


# ----------------------------------------------------------- result normalising

_CENTS = Decimal("0.01")
_PRECISE = Context(prec=100)


def _official_round_number(value: Decimal | float) -> float:
    """2 decimals ROUND_HALF_UP as a float, as the benchmark's normaliser does.

    It quantizes with the default decimal context (precision 28), so infinity
    and a number too large for it raise ``InvalidOperation`` (the run then
    scores 0 as an error) while NaN flows through as a float that equals
    nothing, not even itself. Reproduced on purpose.
    """
    if isinstance(value, Decimal):
        return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))
    return float(Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP))


def _official_round_nested(value: Any) -> Any:
    """Round every number, also inside arrays and objects, before JSON dumps."""
    if isinstance(value, (Decimal, float)):
        return _official_round_number(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_official_round_nested(v) for v in value)
    if isinstance(value, dict):
        return {k: _official_round_nested(v) for k, v in value.items()}
    return value


def _normalize_value_clean(value: Any) -> Any:
    """One cell as our own careful metric sees it: hashable, dates as text."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float):
            if not math.isfinite(value):
                return str(value)
            number = Decimal(repr(value))
        else:
            number = Decimal(value)
        if not number.is_finite():
            return str(number)
        return number.quantize(_CENTS, rounding=ROUND_HALF_UP, context=_PRECISE)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, default=str)
    return value if isinstance(value, str) else str(value)


def normalize_value(value: Any, *, mode: str = OFFICIAL) -> Any:
    """One cell as the chosen metric sees it (``official`` by default).

    Official: dates as ``YYYY-MM-DD``, floats and Decimals rounded to 2 decimals
    as floats (infinity and oversized ones raise), ints/strings/bools/None as
    they are, arrays and objects with their numbers rounded, dumped as JSON.
    """
    if mode == OFFICIAL:
        if isinstance(value, (dt.datetime, dt.date)):
            return value.strftime("%Y-%m-%d")
        prepared = _official_round_nested(value)
        if isinstance(prepared, (dict, list)):
            return json.dumps(prepared, sort_keys=True)
        return prepared
    if mode == CLEAN:
        return _normalize_value_clean(value)
    raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


def normalize_result(
    rows: Sequence[Sequence[Any]], *, mode: str = OFFICIAL
) -> list[tuple[Any, ...]]:
    return [tuple(normalize_value(cell, mode=mode) for cell in row) for row in rows]


def rows_match(
    predicted: Sequence[Sequence[Any]],
    gold: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    mode: str = OFFICIAL,
) -> bool:
    pred, want = (
        normalize_result(predicted, mode=mode),
        normalize_result(gold, mode=mode),
    )
    return pred == want if ordered else set(pred) == set(want)


# --------------------------------------------------------------------- scoring

Executor = Callable[[str, str], "list[list[Any]]"]
"""``(database, sql) -> rows``; raises on any error."""


@dataclass(frozen=True)
class Outcome:
    passed: bool
    reason: str  # match | values | order | no_sql | pred_error | pred_empty | gold_*


class PostgresExecutor:
    """Run one read-only, timeout-bounded statement; one connection per database.

    Read-only and the statement timeout are set on the **connection** with
    ``options=`` at connect time (``default_transaction_read_only`` and
    ``statement_timeout``), not only inside a transaction: a multi-statement
    string could otherwise ``COMMIT`` its way out of a read-only transaction,
    and multi-statement strings are rejected outright by
    ``assert_single_statement`` before anything reaches the server. A
    connection that died (server restart, idle timeout) is dropped from the
    cache and reopened, so one dead connection does not score every later
    query on that database 0.
    """

    def __init__(
        self,
        dsn_template: str,
        timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
        *,
        fetch_cap: int | None = OFFICIAL_FETCH_CAP,
    ) -> None:
        if "{db}" not in dsn_template:
            raise ValueError("the DSN template must contain {db}")
        self.dsn_template = dsn_template
        self.timeout_ms = int(timeout_s * 1000)
        self.fetch_cap = fetch_cap
        self._options = (
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={self.timeout_ms}"
        )
        self._connections: dict[str, Any] = {}

    def _connect(self, database: str) -> Any:
        if database not in self._connections:
            import psycopg

            conn = psycopg.connect(
                self.dsn_template.format(db=database), options=self._options
            )
            self._connections[database] = conn
        return self._connections[database]

    def _discard(self, conn: Any) -> None:
        for db, cached in list(self._connections.items()):
            if cached is conn:
                del self._connections[db]
        try:
            conn.close()
        except Exception:
            pass  # already gone; the cache entry is what mattered

    @staticmethod
    def _is_broken(conn: Any, exc: BaseException) -> bool:
        import psycopg

        if getattr(conn, "closed", False) or getattr(conn, "broken", False):
            return True
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))

    def _fetch(self, conn: Any, sql: str) -> list[list[Any]]:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)  # type: ignore[arg-type]
                if self.fetch_cap is None:
                    rows: list[tuple[Any, ...]] = cur.fetchall()
                else:
                    rows = cur.fetchmany(self.fetch_cap)
                return [list(row) for row in rows]
        finally:
            try:
                conn.rollback()  # ends the transaction, success or not
            except Exception:
                self._discard(conn)  # unusable: the next call reconnects

    def __call__(self, database: str, sql: str) -> list[list[Any]]:
        if not database:
            # an empty name would silently connect to a default database
            # (the user's name), scoring one database's questions on another
            raise ValueError("no database named: the case or report names none")
        assert_single_statement(sql)
        conn = self._connect(database)
        try:
            return self._fetch(conn, sql)
        except Exception as exc:
            if not self._is_broken(conn, exc):
                raise
            self._discard(conn)
            conn = self._connect(database)  # reopen; if this fails, it raises
            return self._fetch(conn, sql)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


@dataclass
class GoldResult:
    rows: list[list[Any]] | None
    error: str | None = None


def run_gold_once(case: Case, execute: Executor, *, mode: str = OFFICIAL) -> GoldResult:
    """Run (and probe) one case's gold query once, under the chosen metric."""
    sql = normalize_sql(case.gold[0].sql, mode=mode)
    try:
        rows = execute(case.target_db, sql)
        # Probe the values too: in official mode a cell the metric cannot
        # normalise (a non-finite number) is a gold problem, not a pred error.
        normalize_result(rows, mode=mode)
        return GoldResult(rows)
    except Exception as exc:  # any database error is a gold problem, not a score
        return GoldResult(None, f"{type(exc).__name__}: {exc}"[:300])


def score_sql(
    case: Case,
    predicted_sql: str | None,
    gold: GoldResult,
    execute: Executor,
    *,
    mode: str = OFFICIAL,
) -> Outcome:
    """Score one predicted query against one case's gold result."""
    if gold.rows is None:
        return Outcome(False, "gold_error")
    if not gold.rows:
        return Outcome(False, "gold_empty")
    if not predicted_sql or not predicted_sql.strip():
        return Outcome(False, "no_sql")
    try:
        rows = execute(case.target_db, normalize_sql(predicted_sql, mode=mode))
    except Exception:
        return Outcome(False, "pred_error")
    if not rows:
        return Outcome(False, "pred_empty")
    try:
        if rows_match(rows, gold.rows, ordered=case.ordered, mode=mode):
            return Outcome(True, "match")
        if case.ordered and rows_match(rows, gold.rows, ordered=False, mode=mode):
            return Outcome(False, "order")
    except Exception:
        # Official behaviour: a value the metric cannot normalise (a
        # non-finite number, say) makes the run an error, score 0.
        return Outcome(False, "pred_error")
    return Outcome(False, "values")


@dataclass
class OfficialScore:
    """Per-question outcomes of one report, with the tier of each question."""

    label: str
    outcomes: dict[str, list[Outcome]]
    tiers: dict[str, str]
    gold_problems: dict[str, str] = field(default_factory=dict)

    def pass_rate(self, case_id: str) -> float:
        runs = self.outcomes[case_id]
        return sum(o.passed for o in runs) / len(runs) if runs else 0.0

    def question_passed(self, case_id: str) -> bool:
        """A question counts as passed when most of its runs did (exact for 1 run)."""
        return self.pass_rate(case_id) > 0.5

    def tier_scores(self) -> dict[str, tuple[int, float]]:
        """Tier -> (questions, mean pass rate), then ``Overall``."""
        groups: dict[str, list[float]] = {}
        for cid in self.outcomes:
            groups.setdefault(self.tiers.get(cid, "unknown"), []).append(
                self.pass_rate(cid)
            )
        order = [t for t in TIERS if t in groups] + sorted(
            t for t in groups if t not in TIERS
        )
        out = {t: (len(groups[t]), sum(groups[t]) / len(groups[t])) for t in order}
        everything = [r for rates in groups.values() for r in rates]
        out["Overall"] = (
            len(everything),
            sum(everything) / len(everything) if everything else 0.0,
        )
        return out


def case_tier(case: Case) -> str:
    """The tier of a case: a ``tier:`` tag, or a plain ``simple``/``moderate``/…"""
    for tag in case.tags:
        if tag.startswith(TIER_TAG):
            return tag[len(TIER_TAG) :]
    for tag in case.tags:
        tier = PLAIN_TIERS.get(tag.lower())
        if tier is not None:
            return tier
    return "unknown"


def match_key(case_id: str) -> str:
    """The id as reports and case files are matched: the ``lsb.`` prefix is noise."""
    return case_id[len(ID_PREFIX) :] if case_id.startswith(ID_PREFIX) else case_id


def _cases_by_key(cases: Sequence[Case]) -> dict[str, Case]:
    by_key: dict[str, Case] = {}
    for case in cases:
        other = by_key.setdefault(match_key(case.id), case)
        if other.id != case.id:
            raise ValueError(
                f"{case.id} and {other.id} differ only by the {ID_PREFIX!r} prefix"
            )
    return by_key


def score_report(
    report: Report,
    cases: Sequence[Case],
    execute: Executor,
    *,
    mode: str = OFFICIAL,
    gold_cache: dict[str, GoldResult] | None = None,
) -> OfficialScore:
    """Score every run of *report* with the chosen metric.

    Case ids are matched with the ``lsb.`` prefix ignored. A case that names no
    database falls back to the report's ``meta.target_db`` (frozen reports were
    run with ``--target-db`` per database and their cases carry no ``database``
    field); that name must then be the Postgres database name.

    The gold query of a case runs once however many reports or runs there are
    (*gold_cache* is shared between reports).
    """
    by_key = _cases_by_key(cases)
    fallback_db = str(report.meta.get("target_db") or "")
    cache = gold_cache if gold_cache is not None else {}
    outcomes: dict[str, list[Outcome]] = {}
    tiers: dict[str, str] = {}
    for cid, result in report.cases.items():
        case = by_key.get(match_key(cid))
        if case is None:
            raise ValueError(f"{cid}: not in the cases files")
        if not case.target_db:
            if not fallback_db:
                raise ValueError(
                    f"{cid}: the case names no database and the report "
                    "records no target_db"
                )
            case = replace(case, target_db=fallback_db)
        if case.id not in cache:
            cache[case.id] = run_gold_once(case, execute, mode=mode)
        tiers[case.id] = case_tier(case)
        outcomes[case.id] = [
            score_sql(case, run.sql, cache[case.id], execute, mode=mode)
            for run in result.runs
        ]
    return OfficialScore(
        label=str(report.meta.get("label", "?")),
        outcomes=outcomes,
        tiers=tiers,
        gold_problems={
            cid: cache[cid].error or "empty result"
            for cid in outcomes
            if cache[cid].rows is None or not cache[cid].rows
        },
    )


# ------------------------------------------------------------------- statistics


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Exact two-sided McNemar p-value from the two discordant counts.

    Under the null each discordant question favours either side with
    probability 1/2, so the p-value is twice the binomial tail of the smaller
    count, capped at 1.
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    smaller = min(only_a, only_b)
    tail = sum(math.comb(n, k) for k in range(smaller + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass(frozen=True)
class Paired:
    both: int
    only_a: int
    only_b: int
    neither: int
    p_value: float


def paired_counts(a: OfficialScore, b: OfficialScore) -> Paired:
    shared = sorted(a.outcomes.keys() & b.outcomes.keys())
    both = only_a = only_b = neither = 0
    for cid in shared:
        pa, pb = a.question_passed(cid), b.question_passed(cid)
        both += pa and pb
        only_a += pa and not pb
        only_b += pb and not pa
        neither += not pa and not pb
    return Paired(both, only_a, only_b, neither, mcnemar_exact(only_a, only_b))


def merge_scores(scores: Sequence[OfficialScore]) -> list[OfficialScore]:
    """Combine the scores of reports that share a label into one score.

    A benchmark run is often saved as one report per database under the same
    label (``main-alien.json``, ``main-museum.json``, ...); each report resolves
    its own ``target_db`` fallback first, so the merge cannot send one
    database's questions to another database. Two reports with the same label
    that hold the same question are an error: relabel one.
    """
    merged: dict[str, OfficialScore] = {}
    for score in scores:
        combined = merged.setdefault(
            score.label, OfficialScore(score.label, {}, {}, {})
        )
        overlap = sorted(combined.outcomes.keys() & score.outcomes.keys())
        if overlap:
            raise ValueError(
                f"reports labelled {score.label!r} both hold "
                f"{', '.join(overlap[:5])}"
                + (" ..." if len(overlap) > 5 else "")
                + "; give one of them its own --label"
            )
        combined.outcomes.update(score.outcomes)
        combined.tiers.update(score.tiers)
        combined.gold_problems.update(score.gold_problems)
    return list(merged.values())


def render_scores(scores: Sequence[OfficialScore]) -> str:
    lines: list[str] = []
    for s in scores:
        lines.append(f"{s.label}")
        for tier, (n, rate) in s.tier_scores().items():
            lines.append(f"  {tier:<12} {rate:6.1%}  ({n} questions)")
        if s.gold_problems:
            lines.append(
                f"  gold unusable for {len(s.gold_problems)} question(s), scored 0: "
                + ", ".join(sorted(s.gold_problems))
            )
    for i, a in enumerate(scores):
        for b in scores[i + 1 :]:
            p = paired_counts(a, b)
            lines.append(
                f"\npaired {a.label} vs {b.label} over "
                f"{p.both + p.only_a + p.only_b + p.neither} shared questions\n"
                f"  both pass {p.both}   only {a.label} {p.only_a}   "
                f"only {b.label} {p.only_b}   neither {p.neither}\n"
                f"  exact McNemar two-sided p = {p.p_value:.4f}"
            )
            if a.outcomes.keys() != b.outcomes.keys():
                lines.append("  WARNING: the reports do not hold the same questions")
    return "\n".join(lines)


# -------------------------------------------------------------------- building


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{n}: {exc}") from exc
    return rows


def format_knowledge(entry: Mapping[str, Any]) -> str:
    """``knowledge: description Definition: definition``."""
    return (
        f"{entry['knowledge']}: {entry['description']} "
        f"Definition: {entry['definition']}"
    )


def load_kb(kb_dir: Path, database: str) -> dict[str, dict[str, Any]]:
    """The knowledge base of one database, keyed by id (as text)."""
    for path in (
        kb_dir / database / f"{database}_kb.jsonl",
        kb_dir / f"{database}_kb.jsonl",
    ):
        if path.is_file():
            return {str(e["id"]): e for e in load_jsonl(path)}
    raise FileNotFoundError(f"no knowledge base for {database!r} under {kb_dir}")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


@dataclass
class BuildResult:
    databases: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.databases.values())


def build_cases(
    public: Sequence[Mapping[str, Any]],
    gt: Sequence[Mapping[str, Any]],
    kb_dir: Path | None,
    *,
    oracle: bool = False,
) -> BuildResult:
    """One case per SELECT task, grouped by database.

    Raises when a SELECT task has no ground truth, or when ``oracle`` is set and
    a knowledge id is missing from the knowledge base: a quietly smaller set
    would be measured as if it were the whole one.
    """
    if oracle and kb_dir is None:
        raise ValueError("--oracle needs the knowledge-base directory")
    truth = {str(row["instance_id"]): row for row in gt}
    result = BuildResult()
    kbs: dict[str, dict[str, dict[str, Any]]] = {}
    missing_truth: list[str] = []
    for task in public:
        gt_row = truth.get(str(task["instance_id"]))
        category = str(task.get("category") or (gt_row or {}).get("category") or "")
        if category.lower() != "query":
            continue
        cid, db = str(task["instance_id"]), str(task["selected_database"])
        if gt_row is None:
            missing_truth.append(cid)
            continue
        sols = gt_row.get("sol_sql") or []
        if isinstance(sols, str):
            sols = [sols]
        if len(sols) != 1:
            result.skipped.append(f"{cid} ({len(sols)} sol_sql)")
            continue
        conditions = _as_dict(gt_row.get("conditions")) or _as_dict(
            task.get("conditions")
        )
        sql = repair_trailing_comma(str(sols[0]))
        case: dict[str, Any] = {
            "id": cid,
            "question": task["query"],
            "database": db,
            "ordered": bool(conditions.get("order", False)),
            "tags": [
                f"{TIER_TAG}{task.get('difficulty_tier') or 'unknown'}",
                f"db:{db}",
            ],
            "gold": [{"label": GOLD_LABEL, "sql": sql}],
        }
        if sql != str(sols[0]):
            case["description"] = "gold repaired: comma before FROM removed"
            result.repaired.append(cid)
        if oracle:
            assert kb_dir is not None
            kb = kbs.setdefault(db, load_kb(kb_dir, db))
            ids = [str(i) for i in gt_row.get("external_knowledge") or []]
            unknown = [i for i in ids if i not in kb]
            if unknown:
                raise ValueError(f"{cid}: knowledge id(s) {unknown} not in the {db} KB")
            if ids:
                case["evidence"] = "\n".join(format_knowledge(kb[i]) for i in ids)
        result.databases.setdefault(db, []).append(case)
    if missing_truth:
        raise ValueError(
            f"{len(missing_truth)} SELECT task(s) have no ground truth: "
            + ", ".join(missing_truth[:10])
        )
    return result


def write_case_files(result: BuildResult, out_dir: Path, *, oracle: bool) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for db in sorted(result.databases):
        path = out_dir / f"{db}.json"
        payload = {
            "source": SOURCE,
            "database": db,
            "oracle": oracle,
            "cases": result.databases[db],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
