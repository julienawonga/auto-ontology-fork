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
| LiveSQLBench-Base-Lite, 180 SELECT tasks x 1 run | 54/180 (30.0%) | 47/180 (26.1%) | Knowledge entries sent as `evidence`; official metric. 15 vs 8 discordant questions, p about 0.21. |

What this says: the hardening work (read-only enforcement, row cap, index
health, key extraction, junction labels, ranking projection) made the engine
safer and more reliable, not more accurate. The Pagila gain measured how well
the changes fit Pagila. A second run of the same code is needed to know how
much one run varies on its own before reading anything into 54 against 47.

LiveSQLBench notes, for whoever runs it again:

- Its gold SQL and knowledge mapping are sent by the BIRD team on request
  (`bird.bench25@gmail.com`, subject `[livesqlbench-base-lite GT&Test Cases]`)
  under a no-redistribution request: keep them, and any case file built from
  them, outside this repository.
- Without the knowledge entries both engines scored 0 on the first 20
  questions: the questions name metrics that only the knowledge base defines.
- Score with the benchmark's own rule, not the harness comparison: strip
  `ROUND(...)` and `DISTINCT` from both queries, run them, round every number
  to 2 decimals, compare rows as sets (as lists when the task requires order).
  The harness rounds to 4 significant figures and fails values that sit on a
  rounding boundary.
- One official gold query (`archeology_7`) has a trailing comma before `FROM`
  and fails on any PostgreSQL; removing that comma is the only fix needed.
- More than about 6 concurrent chat requests on one local stack exhausts the
  backend's database connections and fails questions instantly; replay any
  question that failed in under 5 seconds without SQL, for both engines alike.
