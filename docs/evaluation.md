<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Evaluating the text-to-SQL engine

A change to the engine (prompts, retrieval, validation, the ontology) is judged
by measurement, not by impression. `dev_tools/evals` scores a running backend
against questions whose answers are known, and compares two runs.

## How an answer is scored

The harness sends each question to `POST /api/chat/completions` the way the
frontend does, with no conversation, and compares the **rows the generated SQL
returned** with the expected rows. It never compares SQL text.

Tolerated: column names and order, row order (unless the case is `ordered`),
letter case and whitespace in strings, numbers to four significant digits, a
midnight timestamp for a date.

Not tolerated: a different number of rows or columns, or a different value.

A case can hold several accepted answers when a question has more than one
correct reading, and can opt in to extra or missing columns
(`allow_extra_columns`, `allow_missing_columns`).

Generation is not deterministic, so each question runs several times (three by
default). A question's score is the share of runs that passed, and a report's
score is the mean over questions.

## Commands

Run them from the repository root. Only `httpx` is needed for `run`,
`rescore`, `merge` and `compare`; `snapshot` also needs `psycopg` for a
Postgres source (a SQLite source uses the standard library).

```bash
# Fill or verify the expected rows from the source database
python -m dev_tools.evals snapshot --dsn postgresql://USER:PASSWORD@HOST:5432/pagila
python -m dev_tools.evals snapshot --check --dsn ...

# Score a running backend (about one hour for 13 questions x 3 runs)
python -m dev_tools.evals run --url http://localhost:3001 --label main --out main.json

# Several cases files, several questions at a time (workers capped at 6)
python -m dev_tools.evals run --cases a.json b.json --workers 4 --label main --out main.json

# Re-run only the runs that errored with no SQL in under 5 s (infrastructure)
python -m dev_tools.evals replay main.json --cases a.json b.json

# The same runs under strict scoring, without running anything again
python -m dev_tools.evals rescore main.json --strict --out main-strict.json

# Patch a report with questions that had to be run again (later reports win)
python -m dev_tools.evals merge main.json rerun.json --out merged.json

# Compare a candidate with a baseline; exits 1 when it is rejected
python -m dev_tools.evals compare main.json candidate.json --margin 0.02
```

### What `run` checks and records

Before the first question, `run` reads `GET /api/semantic-compilation/status`
and **refuses to start** (exit 2) when the ontology index is empty or
incomplete for the target database (`--target-db`; without it, for every
database), when the target is not in the list, when the endpoint cannot be read,
or when the backend does not report `index` (an older backend). Fix the index
(`POST /api/semantic-compilation/reindex?database_name=...`), or pass
`--allow-empty-index` to measure anyway; the report then says so.

Every report records in its `meta`: `ontology_index` (per database `expected`,
`indexed`, `missing`), `allow_empty_index` and `ontology_index_problems` when
overridden, `backend_url`, `git_sha` (of this checkout, not of the backend),
`cases_file` and `cases_sha256` (a hash of the cases, independent of
formatting). No documented endpoint reports the backend's version or image, so
that is not recorded: note the image you ran against in the `--label`.

`compare` refuses (exit 2) to compare two reports whose index state differs
(for example one populated, one empty) or whose cases files differ; `merge`
keeps every part's cases hash and index entries, and a part without them makes
the merged report unknown. `--force` compares anyway and prints the mismatch in
the output. A report written before these fields existed is "unknown": it is
compared with a warning. `rescore` stamps the report with the hash of the cases
file it scored against.

`compare` pairs the two reports question by question. A candidate is accepted
when its score gains at least `--margin` **and** no question regresses by more
than `--tolerance`. An improvement on one question does not hide a regression
on another.

A report keeps the rows each run was scored on (up to 50), which is what makes
`rescore` possible.

## Cases

Cases live in `dev_tools/evals/cases/*.json`. Each one has a question, one or
more gold SQL queries, and a snapshot of the rows those queries return.
`snapshot --check` fails when the database no longer matches the snapshot.

A case may also carry `evidence`: text sent with the question as the chat
request's `evidence` field, for context a benchmark supplies (business
definitions). It must never contain the expected answer.

The Pagila set needs the fixture in `dev_tools/fixtures/sql/pagila.sql` loaded
and ingested, with the semantic layer compiled.

### The Chinook set (held out)

`dev_tools/evals/cases/chinook.json` holds 20 questions over the Chinook music
store, a second database with a different shape from Pagila. It was written and
frozen **before any measurement**, so engine changes are not fitted to it. Do
not edit a case, a gold query or an expected row after seeing a result; if a
case is found to be wrong, say so in the report and score every run under the
corrected file.

Load `dev_tools/fixtures/sql/chinook.sql` into a database, ingest it, compile
the semantic layer, then point the run at it with the name it has in the
catalog:

```bash
python -m dev_tools.evals run --cases dev_tools/evals/cases/chinook.json --target-db chinook --label main --out chinook-main.json
```

The expected rows come from the SQLite build of the fixture. To rebuild or
verify them (a `sqlite:///` DSN is opened read-only):

```bash
python -c "import sqlite3; sqlite3.connect('chinook.sqlite').executescript(open('dev_tools/fixtures/sql/chinook.sql', encoding='utf-8-sig').read())"
python -m dev_tools.evals snapshot --check --cases dev_tools/evals/cases/chinook.json --dsn sqlite:///chinook.sqlite
```

Conventions: a ranking question expects the item and its value; a person's
name may be one column or two, and both are accepted as separate gold answers.
A case `description` states what the question means where that is not obvious.

### Freezing a case file

A case file may declare `frozen_at` and `frozen_sha256` (a hash of its `cases`).
`chinook.json` is frozen; `pagila.json` is not. The integrity check
(`python -m dev_tools.integrity`, run by CI) fails when a frozen file's cases no
longer match the hash, so editing a held-out set after the fact is a visible,
reviewable act. To freeze a file, or to accept a deliberate change to one (say
why in the pull request):

```bash
python -m dev_tools.integrity freeze dev_tools/evals/cases/chinook.json
```

The same check scans the shipped code, prompts and frontend for the cases'
questions, expected values and gold-SQL literals, which would let the engine
pass by remembering the test. Questions always fail it. A value that is only a
common word can be listed in `dev_tools/integrity/allowlist.json` with a written
reason; each entry is reviewed like code, and one without a reason is rejected.
See [verification-status.md](./verification-status.md) for what has and has not
been verified for real.

## Rules for an honest measurement

These exist because each of them was broken at least once.

1. **Fix the cases and the scoring convention before measuring.** Do not
   loosen a case after seeing it fail. If a convention must change, say so and
   score every run under it.
2. **Report strict and tolerant scores together.** `rescore --strict` gives the
   strict one from the same runs.
3. **Score both sides with the same cases file.** Re-score the baseline with
   the candidate's cases before comparing.
4. **Do not judge a change only on the questions that motivated it.** Use
   questions it was not written from, ideally on another database.
5. **Check that the ontology is in use.** If the backend logs
   `Retrieved 0 ColumnAttributes ... subject Term None` for every question, the
   semantic index is empty and the run measures the raw schema, not the
   ontology. `run` now enforces this: it refuses an empty or partial index, and
   `compare` refuses reports with different index states.
6. **Keep the machine awake.** A suspended Docker VM stalls the run without
   failing it.
7. **Small differences are noise.** With 13 questions and three runs, one run
   is worth 2.6 points.

## Reference measurements

Pagila, 13 questions x 3 runs, strict scoring, semantic index populated:

| Code | Score |
|---|---|
| `main` before the junction far-side and ranking-projection changes | 61.5% |
| with both changes | 94.9% |

Five questions improved and none regressed. The remaining failure is
"top film category by revenue in store 2", which passes one run in three.

These numbers come from one clean demonstration database and from the
questions that motivated the changes. They are a regression guard, not a claim
about real, messier databases.

### Held-out measurements (2026-11-11)

Run on the local stack against `main` at d92eb4e7 and the code before the
hardening work (81f02eb2), side by side. Neither set was used to change the
engine.

| Set | Before hardening | `main` | Note |
|---|---|---|---|
| Chinook, 20 questions x 3 runs | 70% | 100% | Every earlier failure had the right items and only lacked the value column. Too easy to judge accuracy. |
| LiveSQLBench-Base-Lite, 180 SELECT tasks x 1 run | 54/180 (30.0%; simple 17/42, moderate 25/89, challenging 12/49) | 49/180 (27.2%; simple 13/42, moderate 26/89, challenging 10/49) | Knowledge entries sent as `evidence`; official metric. 14 questions only the old code answered, 9 only the new one (exact McNemar p about 0.40). |

What this says: the hardening work (read-only enforcement, row cap, index
health, key extraction, junction labels, ranking projection) made the engine
safer and more reliable, not more accurate. The Pagila gain measured how well
the changes fit Pagila. A second run of the same code is needed to know how
much one run varies on its own before reading anything into 54 against 49.
An earlier pass reported 47/180 for `main` (p about 0.21): that came from a
throwaway scorer that wrongly failed `crypto_9` and `museum_6`; the numbers
above are from the reviewed `lsb-score` in official mode.

## LiveSQLBench-Base-Lite

The tooling is generic; the data is not ours to ship. The questions are public
(`huggingface.co/datasets/birdsql/livesqlbench-base-lite`:
`livesqlbench_data.jsonl` and `<db>/<db>_kb.jsonl`). The gold SQL and the
knowledge-entry ids per task are sent by the BIRD team on request
(`bird.bench25@gmail.com`, subject `[livesqlbench-base-lite GT&Test Cases]`)
under a **no-redistribution** request. Keep them, and every file built from
them (cases, expected rows, reports that quote gold), **outside this
repository**. `lsb-build` refuses an `--out` inside the checkout. The LiveSQLBench
databases must be loaded in a Postgres you can read.

```bash
# 1. One case file per database, SELECT ("Query") tasks only. --oracle sends each
#    task's knowledge entries as `evidence` ("knowledge: description Definition: definition")
#    and needs --kb-dir; without --oracle no knowledge is needed.
#    --dsn-template also fills the expected rows with the snapshot logic.
python -m dev_tools.evals lsb-build --public livesqlbench_data.jsonl --gt gt.jsonl \
    --kb-dir kb --out ~/lsb --oracle --dsn-template postgresql://USER:PASSWORD@HOST:5432/{db}

# 2. Run, several questions at a time (--workers is capped at 6)
python -m dev_tools.evals run --cases ~/lsb/*.json --workers 4 --repeats 1 \
    --label main --out main.json

# 3. Re-run the infrastructure failures, the same rule for every engine
python -m dev_tools.evals replay main.json cand.json --cases ~/lsb/*.json

# 4. Score with the benchmark's metric; two reports also get a paired test
python -m dev_tools.evals lsb-score --reports main.replayed.json cand.replayed.json \
    --cases ~/lsb/*.json --dsn-template postgresql://USER:PASSWORD@HOST:5432/{db}
```

What each step does and decides:

- **Build.** The question is copied verbatim; `ordered` is the task's
  `conditions.order`; the gold is its single `sol_sql` (a task with several is
  skipped and named in the output, so check the total, 180 for Base-Lite).
  Without `--oracle` no case has `evidence`: the questions name metrics that
  only the knowledge base defines, and without it both engines scored 0 on the
  first 20 questions. One official gold query (`archeology_7`) has a comma
  before `FROM` and fails on any PostgreSQL; the build removes that comma (it
  cannot change a working query) and lists the repaired cases. A missing ground
  truth row or knowledge id is an error, not a smaller test set.
- **Run.** `--cases` takes several files (ids must be unique across them); each
  case goes to the database it names, unless `--target-db` is given. More than
  about 6 concurrent chat requests on one local stack exhaust the backend's
  database connections and fail questions instantly, hence the cap. The report
  stores `workers` and one hash per cases file.
- **Replay.** A run that **errored** (an `error` recorded, or reason `error`),
  **returned no SQL** and took **under 5 seconds** (`--max-seconds`) is an
  infrastructure failure, not an answer. It is run again, and only it. A run
  that produced SQL, that recorded no error, or that took longer is never
  touched, however it scored. The result is written to
  `<report>.replayed.json` (the original is never modified); each replaced run
  is kept under `meta.replay.replaced`. It talks to the `backend_url` the
  report recorded (`--url` overrides it), so run it against the same engine as
  the original. Apply it to every engine you compare, or to none: a replay
  that only helps one side biases the comparison.
- **Score.** `lsb-score` scores saved reports with the benchmark's SELECT
  metric; `--mode` picks which reading of the published rule (see below).
  Case ids are matched with a leading `lsb.` ignored, plain tier tags
  (`simple`, `moderate`, `challenging`) are accepted like `tier:` tags, and a
  case that names no database falls back to the report's `meta.target_db`
  (frozen reports were run per database with `--target-db`). Reports that
  share a label are scored as one -- each report's `target_db` fallback
  applied first, so a run saved as one file per database still gives one set
  of numbers and one paired comparison -- and two same-label reports holding
  the same question are refused. A report whose
  `meta.cases_sha256` matches none of the `--cases` files is refused (it was
  measured on different questions) unless `--allow-cases-mismatch` is passed.
  Both queries run in a read-only, statement-timeout-bounded Postgres session
  (set on the connection itself, and multi-statement strings are rejected
  before anything reaches the server, so `SELECT 1; COMMIT; <anything>` cannot
  escape), the rows are compared as sets, or as lists when the task requires
  an order, and an error or an empty result scores 0. A gold query that errors
  or is empty is listed and scores 0 for everyone. It prints Simple, Moderate,
  Challenging and Overall; given several reports it adds, per pair,
  both/only/neither counts and the exact two-sided McNemar p-value. A question
  counts as passed when most of its runs did, which is exact for one run.

### The two scoring modes

`--mode official` (the default) transcribes the benchmark's own SELECT metric
as it is written -- its regexes and its normaliser, reproduced exactly, quirks
included -- so a score under it is what the benchmark's harness would say.
`--mode clean` is our own, more careful reading of the same published rule.
Every difference between them:

| | `official` | `clean` |
|---|---|---|
| `DISTINCT` | a regex removes every `DISTINCT` not followed (ignoring text up to a parenthesis) by `ON`: `COUNT(DISTINCT x)` loses it, and `IS DISTINCT FROM` becomes a syntax error | only the `SELECT DISTINCT` modifier is removed; `DISTINCT ON`, `COUNT(DISTINCT x)` and `IS DISTINCT FROM` keep their meaning |
| `ROUND` | `ROUND(x, n)` **and** `ROUND(x)` are replaced by the bare `x`, no parentheses added, so `2 * ROUND(a + b, 2)` becomes `2 * a + b` | `ROUND(x, n)` becomes `(x)` (precedence kept); `ROUND(x)` stays |
| comments | plain regexes, not quote-aware: `--` inside a string literal mangles the query, and a trailing `--` comment with no newline after it is **not** removed | a quote-aware tokenizer removes real comments only |
| whitespace | collapsed to single spaces everywhere, **including inside string literals** | untouched inside literals |
| rows read | at most 10,000 per query (`fetchmany`) | all of them |
| numbers | floats and Decimals rounded to 2 decimals ROUND_HALF_UP as floats; ints untouched; numbers **inside** arrays and JSON are rounded before dumping | every number (ints too) becomes a Decimal at cents; nested values are dumped as they came |
| non-finite numbers | infinity (and numbers too large for the decimal context) makes the quantize raise, so the run scores 0 as an error; NaN flows through as a float that equals nothing, not even itself | they compare as the text `nan`/`inf` |
| trailing `;` | kept | stripped |

Use `official` for any number you want to compare with the benchmark's own
results; `clean` when you want the metric to judge the query's meaning, not
its spelling. On the frozen held-out reports (re-scored 2026-11-12 with the
fixed `lsb-score`) the two modes agreed on every one of the 180 questions for
both engines; the five gold queries that are unusable (they return no rows)
are the same in both.

- This is our own implementation of the published rule (both modes), not the
  benchmark's code, and it has been checked against synthetic data and the
  frozen held-out reports. Before quoting a score as "official", compare it on
  a few tasks with the benchmark's own harness.

The harness's own comparison (`run`, `rescore`) rounds to 4 significant figures
and fails values on a rounding boundary; use it for a quick signal and
`lsb-score` for the number you report.
