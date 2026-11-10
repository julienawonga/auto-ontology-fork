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
`rescore`, `merge` and `compare`; `snapshot` also needs `psycopg`.

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

The Pagila set needs the fixture in `dev_tools/fixtures/sql/pagila.sql` loaded
and ingested, with the semantic layer compiled.

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
   ontology.
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
