# SPDX-License-Identifier: Apache-2.0

"""LiveSQLBench-Base-Lite: build case files, and score with the benchmark's rule.

The questions are public; the gold SQL and the knowledge-entry ids come from the
BIRD team under a **no-redistribution** request. Nothing here reads them from, or
writes them to, the repository: every function takes paths, and ``lsb-build``
refuses an output directory inside the checkout.

``build_cases`` turns the SELECT ("Query") tasks into one case file per database.
``official_match`` re-implements the benchmark's SELECT metric (our own code):

1. strip comments, ``DISTINCT`` (but not ``DISTINCT ON``) and ``ROUND(x, n)`` from
   the predicted *and* the gold query,
2. run both in a read-only transaction under a statement timeout,
3. normalise the rows (dates to ``YYYY-MM-DD``, every number to 2 decimals with
   ROUND_HALF_UP, nested values as JSON text),
4. compare as sets, or as lists when the task requires an order.

An error or an empty result on either side scores 0.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Context, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from dev_tools.evals.cases import Case
from dev_tools.evals.report import Report

SOURCE = "livesqlbench-base-lite"
TIERS = ("Simple", "Moderate", "Challenging")
TIER_TAG = "tier:"
GOLD_LABEL = "gold"
DEFAULT_STATEMENT_TIMEOUT_S = 60.0

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


def strip_distinct(sql: str, *, aggregates: bool = False) -> str:
    """Drop the ``SELECT DISTINCT`` modifier; ``DISTINCT ON`` stays.

    ``COUNT(DISTINCT x)`` and ``IS DISTINCT FROM`` are left alone: removing them
    changes what the query computes, not just its row set. ``aggregates=True``
    also strips the aggregate form, for a benchmark rule that does.
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
        if before == "SELECT" or (aggregates and before == "("):
            tokens[i] = ("ws", "")
    return _join(tokens)


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


def normalize_sql(sql: str, *, strip_aggregate_distinct: bool = False) -> str:
    """The query as the benchmark's metric runs it."""
    out = strip_round(
        strip_distinct(strip_comments(sql), aggregates=strip_aggregate_distinct)
    )
    return out.strip().rstrip(";").strip()


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


def normalize_value(value: Any) -> Any:
    """One cell as the metric sees it: hashable, dates as text, numbers to cents."""
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


def normalize_result(rows: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(normalize_value(cell) for cell in row) for row in rows]


def rows_match(
    predicted: Sequence[Sequence[Any]],
    gold: Sequence[Sequence[Any]],
    *,
    ordered: bool,
) -> bool:
    pred, want = normalize_result(predicted), normalize_result(gold)
    return pred == want if ordered else set(pred) == set(want)


# --------------------------------------------------------------------- scoring

Executor = Callable[[str, str], "list[list[Any]]"]
"""``(database, sql) -> rows``; raises on any error."""


@dataclass(frozen=True)
class Outcome:
    passed: bool
    reason: str  # match | values | order | no_sql | pred_error | pred_empty | gold_*


class PostgresExecutor:
    """Run SQL read-only under a statement timeout; one connection per database."""

    def __init__(
        self, dsn_template: str, timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S
    ) -> None:
        if "{db}" not in dsn_template:
            raise ValueError("the DSN template must contain {db}")
        self.dsn_template = dsn_template
        self.timeout_ms = int(timeout_s * 1000)
        self._connections: dict[str, Any] = {}

    def _connect(self, database: str) -> Any:
        if database not in self._connections:
            import psycopg

            conn = psycopg.connect(self.dsn_template.format(db=database))
            conn.read_only = True  # BEGIN READ ONLY: a write fails in the server
            self._connections[database] = conn
        return self._connections[database]

    def __call__(self, database: str, sql: str) -> list[list[Any]]:
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {self.timeout_ms}")
                cur.execute(sql)  # type: ignore[arg-type]
                return [list(row) for row in cur.fetchall()]
        finally:
            conn.rollback()  # ends the transaction, success or not

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


@dataclass
class GoldResult:
    rows: list[list[Any]] | None
    error: str | None = None


def run_gold_once(
    case: Case, execute: Executor, *, strip_aggregate_distinct: bool = False
) -> GoldResult:
    sql = normalize_sql(
        case.gold[0].sql, strip_aggregate_distinct=strip_aggregate_distinct
    )
    try:
        return GoldResult(execute(case.target_db, sql))
    except Exception as exc:  # any database error is a gold problem, not a score
        return GoldResult(None, f"{type(exc).__name__}: {exc}"[:300])


def score_sql(
    case: Case,
    predicted_sql: str | None,
    gold: GoldResult,
    execute: Executor,
    *,
    strip_aggregate_distinct: bool = False,
) -> Outcome:
    """Score one predicted query against one case's gold result."""
    if gold.rows is None:
        return Outcome(False, "gold_error")
    if not gold.rows:
        return Outcome(False, "gold_empty")
    if not predicted_sql or not predicted_sql.strip():
        return Outcome(False, "no_sql")
    try:
        rows = execute(
            case.target_db,
            normalize_sql(
                predicted_sql, strip_aggregate_distinct=strip_aggregate_distinct
            ),
        )
    except Exception:
        return Outcome(False, "pred_error")
    if not rows:
        return Outcome(False, "pred_empty")
    if rows_match(rows, gold.rows, ordered=case.ordered):
        return Outcome(True, "match")
    if case.ordered and rows_match(rows, gold.rows, ordered=False):
        return Outcome(False, "order")
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
    return next(
        (t[len(TIER_TAG) :] for t in case.tags if t.startswith(TIER_TAG)), "unknown"
    )


def score_report(
    report: Report,
    cases: Sequence[Case],
    execute: Executor,
    *,
    strip_aggregate_distinct: bool = False,
    gold_cache: dict[str, GoldResult] | None = None,
) -> OfficialScore:
    """Score every run of *report* with the benchmark's metric.

    The gold query of a case runs once however many reports or runs there are
    (*gold_cache* is shared between reports).
    """
    by_id = {c.id: c for c in cases}
    cache = gold_cache if gold_cache is not None else {}
    outcomes: dict[str, list[Outcome]] = {}
    for cid, result in report.cases.items():
        case = by_id.get(cid)
        if case is None:
            raise ValueError(f"{cid}: not in the cases files")
        if not case.target_db:
            raise ValueError(f"{cid}: the case names no database")
        if cid not in cache:
            cache[cid] = run_gold_once(
                case, execute, strip_aggregate_distinct=strip_aggregate_distinct
            )
        outcomes[cid] = [
            score_sql(
                case,
                run.sql,
                cache[cid],
                execute,
                strip_aggregate_distinct=strip_aggregate_distinct,
            )
            for run in result.runs
        ]
    return OfficialScore(
        label=str(report.meta.get("label", "?")),
        outcomes=outcomes,
        tiers={cid: case_tier(by_id[cid]) for cid in outcomes},
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
