<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Experiments log

A chronological lab notebook of every measurement and key engineering finding on
Lexis since the fork was cloned (2026-11-09) up to 2026-11-12. It is written so a
maintainer or reviewer can decide how far to trust each claim and, where
possible, repeat it.

## 1. Purpose and how to read

- Each entry says what question was asked, how it was set up, what came out, what
  statistics were applied, what the entry does **not** show, and where the raw
  data lives.
- **Tags** use the levels of [verification-status.md](./verification-status.md):
  `REAL-E2E` (real stack: real database, real model endpoint, through the path a
  user takes), `REAL-ENGINE` (real database engine, not the full stack), `FAKES`
  (unit tests with stubs), `UNVERIFIED`.
- **Manual run, not repeatable from the repository** means the run needed data
  or a stack that the repository does not contain (LiveSQLBench, the
  maintainer's local stack and model gateway). The procedure is in section 6; the
  reports are kept outside the repository.
- **Development vs held-out.** A question set used while looking at failures is a
  development set and is not evidence of generalisation. Held-out sets (Chinook,
  LiveSQLBench-Base-Lite) were frozen before the engine was measured on them.
  Each entry says which kind it is.
- Numbers come from the sources named in the entry: a pull request body (`#N`,
  repository `julienawonga/lexis`), a document in this repository, the score files
  of the maintainer's machine, or the maintainer's working notes. Where a number
  could not be checked against a primary source it is marked **unverified**.
- Times are UTC and approximate where the notes give only an hour.
- LiveSQLBench gold SQL, knowledge-base text, question text and expected rows are
  never reproduced here (the benchmark's authors asked for no redistribution).
  Only aggregate scores, counts, p-values and case ids appear.

## 2. Method summary

The full method is in [evaluation.md](./evaluation.md); only what a reader needs
to interpret the entries is repeated.

- **Scoring.** The harness compares the rows the generated SQL returned with
  expected rows, never SQL text. Each question runs several times (three by
  default for Pagila and Chinook; one for LiveSQLBench); a report's score is the
  share of runs passed, and a question counts as passed when most runs did.
- **Frozen sets.** `chinook.json` is frozen (hash checked by CI,
  `python -m dev_tools.integrity`). The LiveSQLBench case files were frozen
  (`sha256sum -c`) before any measurement. `pagila.json` is not frozen and is a
  development set.
- **Official vs clean metric** (LiveSQLBench). `lsb-score --mode official`
  transcribes the benchmark's SELECT metric as written; `--mode clean` is a more
  careful reading. On the frozen reports of entry E09 both modes gave the same
  answer on all 180 questions (`clean.txt` and `official.txt`: identical counts).
  All LiveSQLBench numbers below are `official` unless stated.
- **Paired test.** Two engines are compared question by question with the exact
  two-sided McNemar test on the discordant pairs, at alpha 0.05.
- **Noise band.** Two runs of the same generation code on the same 180 questions
  gave 49 and 56 correct (entry E13), so differences of about 4 points
  (7 questions of 180) are inside run-to-run noise.
- **Rules** (evaluation.md, "Rules for an honest measurement"): cases and scoring
  are fixed before measuring; no case is edited and no convention loosened after
  seeing a result; strict and tolerant scores are reported together; both sides
  use the same cases file; a change is not judged only on the questions that
  motivated it; the semantic index must be populated.

## 3. Chronological log

### E00 - Pagila reference answers (2026-11-09)

- **Question.** What are the true answers to the Pagila questions, so the chat
  can be graded?
- **Setup.** SQL run directly in Postgres on the committed fixture
  (`dev_tools/fixtures/sql/pagila.sql`: 599 customers, 1000 films, 599 rentals,
  599 payments). Source: maintainer note "pagila-ground-truth".
- **Result.** 13 reference answers, for example top spender Ruth Martinez 23.95,
  total revenue 2502.01, category with most films Sports (74), and the hard
  question "top category by revenue in store 2" = Drama 104.79 (by inventory
  store). Traps found: rentals only span 2022-05-24..28 while payments span
  2022-01..07; "store 2" is ambiguous (inventory store vs customer store).
- **Limits.** The gold SQL and the questions were written by the same author, so
  the answers are not independent. The user was asked to review ambiguous ones;
  there is no record in the sources that the Pagila answers were reviewed.
  Pagila is a development set.
- **Where.** `dev_tools/evals/cases/pagila.json`.

### E01 - First Pagila baseline, 79.5% (2026-11-10): invalid, corrected

- **Question.** What does the engine score on Pagila before any change?
- **Setup.** Harness of PR #10, 13 questions x 3 runs.
- **Result.** 79.5% (PR #10 body, "Baseline recorded 2026-11-10 before the SQL
  guard and row limit").
- **What was wrong.** (1) The semantic index was **empty** (entry E02), so the run
  measured the raw schema, not the ontology (register row "Evaluation
  baselines"). (2) The maintainer notes record that the grader had been loosened
  after seeing failures (`allow_missing_columns`; "64.1% -> 79.5% with no engine
  change") and one question reworded to avoid ties. The figures 64.1% and the
  loosening come from the notes only (**unverified** against a report; the
  report was not kept).
- **Correction.** The user objected that the test was being fitted to the
  result. Rules 1, 2 and 5 of evaluation.md came from this. The cases were made
  strict again in PR #15 (four cases that tolerated a missing column were
  restored, declared before measuring) and the run now refuses an empty index.
  **Never compare against the 79.5% baseline.**
- **Where.** Register row "Evaluation baselines"; reports not committed.

### E02 - The semantic index was empty (2026-11-10)

- **Question.** Why did chat answer from the raw schema?
- **Finding.** `vdb.semantic_layer` held no rows: the semantic embedder built its
  parameters by hand and ignored `EMBED_EXTRA_FIELDS` / `EMBED_DIMENSIONS`, so the
  gateway rejected every row (`embed_all_semantic_nodes: 0/80 rows had
  embeddings`), while compilation was still recorded as `succeeded` (21 times).
  Every chat question retrieved 0 candidates.
- **Fix.** PR #11 (both call sites use `get_embed_params()`), PR #18 (a compile
  that leaves rows out is recorded as failed; `POST
  /api/semantic-compilation/reindex`; per-database `expected/indexed/missing`;
  WARNING on empty retrieval), PR #29 (eval pre-flight refuses an empty index).
- **Verification.** Reproduced against the live gateway with a dry run (PR #11).
  After the fix the status read 80 expected, 80 indexed, 0 missing on the running
  stack (register, `REAL-E2E`). The reindex route itself was only tested with
  fakes (`FAKES`).

### E03 - Pagila: junction far-side table and ranking projection, 61.5% to 94.9% (2026-11-10)

- **Question.** Do PR #13 (include the label table on the far side of a kept
  junction) and PR #15 (return the ranked value with the item) improve answers?
- **Setup.** Pagila, 13 questions x 3 runs, strict scoring, semantic index
  populated, same cases file on both sides, `main` against the candidate with
  both changes (PR #15 body).
- **Result.** 61.5% to 94.9%; harness verdict ACCEPT; 5 questions improved, 0
  regressed. One question still fails: top category by revenue in store 2 (1 run
  in 3).
- **Limits.** Both changes were written from these very questions: this is a
  **development** result and measures how well the changes fit Pagila. The notes
  also quote a different pre-change figure ("56.4% strict / 74.4% tolerant");
  the two figures were measured at different times and are not reconciled here
  (**unverified**). A reviewer should treat 61.5% as the figure of record
  (PR #15, docs/evaluation.md). E08 and E09 later showed the gain did not carry
  over.
- **Where.** `dev_tools/evals/cases/pagila.json`; report label
  `candidate-junction+projection` (not committed). Tag `REAL-E2E`.

### E04 - Postgres foreign-key extraction lost keys (2026-11-10)

- **Question.** Does the catalog hold the real FKs of Pagila?
- **Finding.** On Pagila `payment` has its three FKs only on its seven
  partitions, so the catalog held 0 keys touching `payment` and the Payment Term
  was isolated. Three more defects: a 2-column FK produced 4 rows; constraint
  names are unique per table, not per schema; and (found by CI) cross-schema FKs
  such as `analytics.film_performance -> public.film` were dropped.
- **Result.** After PR #16: 21 foreign keys instead of 36 rows, including
  `payment -> customer`, `rental`, `staff`; `payment` reports its primary key
  `(payment_date, payment_id)`.
- **Limits.** Postgres 17 only. A catalog ingested before the fix keeps the old
  keys until re-ingested; the running catalog was not re-ingested at the time.
  The notes judge the effect on answers small (the model infers joins from
  column names); the effect was not measured.
- **Where.** `auto_ontology/connectors/tests/test_postgres_keys.py` (live Postgres
  tests; `REAL-ENGINE`).

### E05 - Value matching probes and the value index (2026-11-10 to 2026-11-11)

- **Question.** Does the chat find stored values when the user types them
  differently?
- **Finding (2026-11-10).** "customers from the USA" returned 0 (stored value
  "United States", truth 36); "customers in Indai" returned 0 (truth 60). Case
  differences already worked (pg-13, mary smith, drama). Setting the opt-in
  `DB_PROBE_PROACTIVE` changed nothing, because the literal check only proposes
  values for columns under 20 distinct values (countries 109, cities 599).
  Measured on a throwaway backend (PR #25 body).
- **Change.** PR #25: opt-in value index, lookup, generic ISO alias table
  (all 249 countries and US state codes).
- **Live probe (2026-11-11, stack with the value index built for Pagila).**
  "USA" 36, "US" 36 (before 0), "pg-13" 223, "Norway" 0 (not corrected), "Indai"
  **failed** (anchor "India" found, generated SQL nonsense, no rows after 227 s)
  (register row; PR #25 and notes).
- **Privacy incident.** The first real index build for Pagila (5,954 values, 19
  columns) wrongly included customer e-mails, phones, names, addresses and
  `staff.password` because PII detection was off. 3,847 rows were deleted by hand;
  name and value-shape rules were added (PR #30). 2,107 non-personal values
  remained. A personal value under an ordinary column name with ordinary-looking
  values would still not be detected.
- **Limits.** The five questions are the ones the feature was designed from, so
  this is **not** evidence of generalisation. A held-out set,
  `dev_tools/evals/cases/chinook_values.json`, exists and has never been run.
  Tag `REAL-E2E` for the probe, manual, not repeatable from the repository.

### E06 - Business term "VIP customer" (2026-11-11)

- **Question.** Is a defined business term (a manual SQL attribute) used by the
  chat?
- **Setup.** Pagila, attribute created through the API, questions sent with
  `target_db=pagila`, attribute deleted afterwards (gone from the table and the
  vector index).
- **Result.** First probe: used for "Which customers are VIP?" (correct), ignored
  for "How many VIP customers do we have?" (599, all customers). Without the
  definition the engine invented a different threshold each time (143, then 382;
  truth with a threshold of 15 is 22). Re-run at 09:44-09:46: 22, 22, 22 (3 of 3
  correct, attribute used); the 599 was **not reproduced**.
- **Isolated check.** A hand-built two-table context with the attribute: the
  generation agent used it 12 of 12 times (count 22) whatever the subtype, so
  generation is not the cause. No code fix was justified; the cause of the first
  599 is unknown.
- **Limits.** Small sample (3 and 12 runs), development question. Register row
  "Business definition used by the chat" (`REAL-E2E`, manual). The separate row
  "Business metrics through SQL attributes" stays `UNVERIFIED`.

### E07 - The engine learned a test question (2026-11-11)

- **Finding.** Semantic compilation ran `suggest_sql_attributes`, which mines past
  query SQL and writes the SQL attributes an LLM picks into the shared ontology
  without review. Two appeared overnight ("Total Payment Amount", "Payment Amount
  At Least 15"); the second was the threshold typed into the VIP test question.
  That contaminates evaluations and writes shared knowledge without a human.
- **Action.** Both deleted (with the user's consent); PR #31 puts the stage behind
  `sql_attribute_suggestion_enabled`, off by default. A compilation on the running
  stack logged the stage as off and wrote no attribute (`REAL-E2E`).
- **Why it matters.** It is the concrete case behind the design rule that
  learned knowledge is a draft until a human certifies it
  ([design/self-improving-ontology.md](./design/self-improving-ontology.md)).

### E08 - Chinook, held-out, old code against current (2026-11-11 ~10:15-10:45)

- **Question.** After the Pagila work, does the engine still hold on a database it
  was not tuned on, and did the hardening regress anything?
- **Setup.** `dev_tools/evals/cases/chinook.json`, 20 questions, frozen before
  measuring, 3 runs each. Chinook loaded into the stack's Postgres (11 tables, 64
  columns; compiled, index 63/63). Arms: `main` at d92eb4e7 and the pre-hardening
  image at 81f02eb2, run as a temporary side container (`--allow-empty-index`
  because the old image has no status route; its index was healthy).
- **Result.** `main` 100% (20/20); pre-hardening 70%. Harness verdict ACCEPT, 6
  improved, 0 regressed.
- **Reading.** All 6 baseline failures returned the right items and lacked only the
  value column, which is the ranking-projection rule (PR #15) that the Chinook
  cases require. Substantive accuracy is about 100% on both: the set separates
  presentation, not reasoning. It is too easy and too well known to models to
  judge accuracy.
- **Limits.** The 20 expected answers were re-checked against SQLite (20 of 20
  match) but a human has **not reviewed** them; watch `playlist_most_tracks` (two
  playlists share a name) and `best_customer`. The cases were written by the same
  author as the gold. Reports kept in the maintainer's session scratch directory,
  not committed. `REAL-E2E`, manual.

### E09 - LiveSQLBench-Base-Lite, held-out, old code against `main` (2026-11-11)

- **Question.** Did the hardening work (PRs #8-#19) make the engine more accurate
  on a real, held-out benchmark?
- **Setup.** 180 SELECT tasks, 18 PostgreSQL databases, 1 run each, knowledge-base
  entries sent as `evidence` (the benchmark's "oracle knowledge" setting), official
  metric, 90 management (CRUD) tasks skipped because the engine is read-only. Case
  files built by the maintainer's script from the benchmark's public questions
  and the ground truth supplied by the benchmark authors on request; frozen
  before any measurement (`cases.FROZEN.sha256`, `cases-oracle.FROZEN.sha256`,
  both verified 18/18 after the run). One official gold query (`archeology_7`)
  has a stray comma before `FROM`; it was removed and tagged. Five gold queries
  return nothing (`insider_1`, `vaccine_2`, `vaccine_7`, `vaccine_10`,
  `virtual_2`) and score 0 for every engine. Arms: `main` at d92eb4e7 and the
  image at 81f02eb2.
- **Result.** Baseline 54/180 (30.0%; simple 17/42, moderate 25/89, challenging
  12/49). `main` 49/180 (27.2%; 13/42, 26/89, 10/49). Discordant: 14 only the
  baseline, 9 only `main`; exact McNemar p = 0.4049 (`official.txt`).
- **Statistics.** Not significant. `clean` mode gave the same counts.
- **Detours and mistakes (kept on purpose).**
  1. A first pass **without** the knowledge base scored 0% for both engines on
     2 databases (alien, archeology): terms such as SNQI exist only in the KB, so
     it was uninformative and was stopped. It was redone in oracle setting.
  2. The harness comparison (4 significant figures, rounding boundary) marked
     correct answers wrong on this benchmark, so harness scores were discarded in
     favour of the official metric.
  3. A throwaway scorer reported `main` 47/180 (PR #32 body: p about 0.21,
     15 vs 8 discordant). It wrongly failed `crypto_9` and `museum_6`. The
     reviewed `lsb-score` (PR #33) and a literal transcription of the benchmark's
     code both give 49. PR #32 recorded 47; docs/evaluation.md now says 49 and
     explains the correction.
  4. Running 14 worker processes in parallel caused infrastructure failures (the
     backend could not get a database connection: main 23 questions, baseline 19,
     in under 5 seconds with no SQL). Exactly those were replayed under one rule
     for both engines (no SQL, under 5 s) and merged; the originals are kept in
     `reports-oracle/pre-replay/`.
  5. Interim reading at 110 paired questions was 37 vs 37; at 150, 44 vs 42. The
     final figures above are for all 180.
- **Conclusion.** The hardening made the engine safer and more reliable, not more
  accurate; the Pagila gain measured fit to Pagila (E03).
- **Limits.** Single run per question; the scorer is our transcription of the
  benchmark rule and was not compared with the benchmark's own harness on more
  than a few tasks (docs/evaluation.md says so). The comparison is between code
  versions with different engine behaviour, not an isolated single change.
- **Where.** Reports and scores in the maintainer's `livesqlbench/` directory,
  outside the repository: `reports-oracle/`, `pr33-check/fixed/official.txt`,
  `clean.txt`. Recorded in the repository by PR #32 and PR #33.

### E10 - Source-database connections: exhaustion and fix (2026-11-11)

- **Finding 1.** Each registered source database held about 4 idle Postgres
  connections (backend and ingestion pools). Twenty databases exhausted
  `max_connections=100` and blocked the whole stack. The local Postgres was raised
  to 400 as a workaround.
- **Finding 2.** Under load the backend failed instantly instead of queueing
  ("missing required 'connectors'", "couldn't get a connection after 30 sec"); an
  unreadable connection store was returned as an empty connector list.
- **Fix.** PR #34: lazy pool with minimum 0 and idle close, per-source and
  per-process cap with waiting, `ConnectorLoadError` instead of `[]`.
- **Verification.** CI tests use a real PostgreSQL: 20 sources show 0 server-side
  connections before use, at most 2 per database under load, 0 after the idle
  timeout; 24 concurrent statements through a cap of 3 all succeed (`REAL-ENGINE`).
  On the stack with the image built from the PR branch (maintainer notes, 2026-11-11
  evening): idle source connections 60 to 0; 16 concurrent chats gave 0 connection
  errors, max 2 connections per database, but 15 of 16 produced no SQL because the
  test question needed system tables the catalog validator rejects (an engine
  limit, not the pool); 6 concurrent real LiveSQLBench questions gave 6/6 executed
  SQL, 0 errors, max 1 connection per database.
- **Limits.** The original 14-way failure mode was not replayed at full scale.

### E11 - The host crashed under 18 concurrent chats (2026-11-11 20:45)

- **Finding.** 18 concurrent chats plus another tool running produced a Windows
  blue screen (0x133, DPC_WATCHDOG_VIOLATION). Cause identified from the code: the
  backend started one full Python subprocess per concurrent conversation with no
  cap (`server/chat/worker.py`). The host has 13.6 GB and the Docker VM about 7 GB.
- **Rule adopted.** Load tests use at most 8 concurrent chats and never run
  alongside builds.
- **Fix.** PR #35 (`CHAT_MAX_CONCURRENT`, default 4; wait up to `CHAT_WAIT_S`, then
  503 with `Retry-After`). Live test before merge (notes): 10 questions at cap 4,
  merged only after "max in_use <= 4, worker processes <= 6, no crash, no 503
  storm"; the measured values are not given in the notes (**unverified**).
  Register row is `FAKES` for the unit tests.

### E12 - Gateway 429 storm from a 54-question burst (2026-11-11 ~23:15)

- **Finding.** On a temporary cloud VM (since deleted) 12 concurrent chats were
  fine; 54 concurrent made the model gateway's edge return HTML 429 and then block
  the VM's IP. The engine amplified the burst: embeddings 32-wide per call, SDK
  retries times application retries times a fallback model. A second finding:
  cancelled conversations kept calling the model after the client disconnected
  (about 100 gateway calls per 20 s after the eval client was killed; the backend
  had to be restarted).
- **Fix.** PR #38 (backoff with jitter and `Retry-After`, no nested retries,
  process-wide in-flight cap 8, embedding concurrency 4); PR #40 (disconnect
  cancels stateless runs, conversation runs after 60 s without a reader, any run
  after `CHAT_MAX_RUN_S` = 300 s; eval runner waits out 503).
- **Limits.** Both are `FAKES` in the register: the 429 burst and the 100-calls
  incident were not reproduced; the root cause of the latter is from code reading
  plus fake-based tests (PR #40 body). Rule: at most about 12 concurrent chats per
  IP on this gateway. The 300 s limit appears later as the error in the language
  runs (E20).

### E13 - Noise: the same engine twice (2026-11-12 ~00:30-03:40)

- **Question.** How much does one 180-question run vary on its own?
- **Setup.** `main` again, label `main-02d166c0-noise`, same frozen oracle cases,
  1 run, 2 workers, official metric. Script `noise_run.sh`.
- **Result.** 56/180 (31.1%; simple 17/42, moderate 28/89, challenging 11/49)
  against the first run's 49/180. Discordant 13 only the new run, 6 only the old;
  exact McNemar p = 0.1671. Against the baseline 81f02eb2 (54/180): 10 vs 8,
  p = 0.8145 (`noise.txt`).
- **Conclusion.** Run-to-run noise is about 4 points (7 questions). The 54-vs-49
  gap of E09 is noise: no regression is shown, no improvement either.
- **Limits.** The two `main` runs are different commits (d92eb4e7 and 02d166c0,
  which adds PRs #32-#34: tooling and connection handling). The notes state that
  generation logic did not change between them; this was not independently
  diffed for this document (**unverified**). One run per question.
- **Where.** `reports-noise/`, `pr33-check/fixed/noise.txt` (outside the repo).

### E14 - Arm A: no knowledge anywhere (2026-11-12, finished 02:47)

- **Question.** What does the engine score on LiveSQLBench with no business
  knowledge at all?
- **Setup.** Frozen `cases/` (no evidence), 180 tasks, 1 run, 2 workers, label
  `main-dfbea133-nokb`. Script `nokb_run.sh`. First pass finished 13/18 databases;
  5 failed because `uv` could not fetch `httpx` from PyPI (DNS error, not an
  engine failure) and were run again.
- **Result.** 3/180 (1.7%; simple 3/42, moderate 0/89, challenging 0/49). Against
  arm B (first run, 49/180): both 2, only A 1, only B 47, neither 130; exact
  McNemar p < 1e-4 (`armA-vs-B.txt`).
- **Limits.** Arm B was measured on `cases-oracle/` and arm A on `cases/`; the
  scorer prints a hash-mismatch warning for that (the files differ only by the
  `evidence` field) and the comparison was run with
  `--allow-cases-mismatch`. The two arms are different commits (d92eb4e7,
  dfbea133).

### E15 - Arm C: knowledge loaded as Terms, definitions not given to generation (2026-11-12, finished 06:25)

- **Question.** Does knowledge stored in the ontology help as much as knowledge
  pasted into the question?
- **Setup.** `lsb-kb` (PR #41) loaded each database's KB entries as uncertified
  Terms for the 18 LiveSQLBench databases only (dry run: 1,062 Terms would be
  created, 0 conflicts; about 1,054 applied, the rest skipped because Term names
  are unique across databases; counts from the notes, **unverified** against the
  tool's log). Backups of each database's model were exported first; Pagila and
  Chinook were untouched (index counts 80 and 63 checked). Frozen `cases/` (no
  evidence), 1 run, 4 workers, label `main-2467fe3b-kbterms`. Script
  `kbterms_run.sh`. Run only after arm A finished, to avoid contamination.
- **Result.** 4/180 (2.2%; simple 3/42, moderate 1/89, challenging 0/49), the
  same as arm A (both 2, only C 2, only A 1; p = 1.0), and far below arm B
  (only C 3, only B 48; p < 1e-4) (`armC.txt`).
- **Root cause (from code).** Retrieval kept one "subject Term" and used it only to
  add its linked tables to the prompt; Term descriptions and definitions never
  reached the SQL generator, which consumed only column attributes and SQL
  attributes. The ontology's business definitions were invisible to generation.
- **Side finding.** One Solar question looped through reconstruct/validate for
  about 50 minutes because the deployed engine had no run-time limit yet (PR #40
  adds `CHAT_MAX_RUN_S`).
- **Limits.** The KB entries were loaded through the whole-model import route
  (no Term create/delete API existed then, which led to PR #45). Arm C also
  changes which tables are offered, which arm B does not.

### E16 - Arm C': Term definitions given to generation (2026-11-12 ~07:00-10:10)

- **Question.** If the relevant Terms' definitions reach SQL generation, does the
  ontology carry the knowledge as well as pasted evidence?
- **Setup.** PR #42 (flag `TERM_DEFINITIONS_IN_GENERATION`, built from commit
  686adb04, deployed as a separate image with the flag on; 7-9 Terms per question
  injected). Same Terms and frozen `cases/` as arm C, 1 run, 4 workers, label
  `pr42-686adb04-cprime`. Script `cprime_run.sh`, override file
  `cprime-override.yml`.
- **Interim (07 of 18 databases, 70 questions).** C' 25/70 (35.7%) against C 2/70,
  B 26/70, B-noise 25/70; C' vs C 24 to 1, p < 1e-4; C' vs B 5 vs 6, p = 1.0
  (notes; **unverified** against a file, the interim output was not kept).
- **Final (180).** C' 55/180 (30.6%; simple 20/42, moderate 26/89, challenging
  9/49). C' vs C: both 3, only C' 52, only C 1, p < 1e-4. C' vs B (first run, 49):
  both 34, only C' 21, only B 15, p = 0.4050. C' vs B-noise (56): only C' 15, only
  B-noise 16, p = 1.0 (`armCprime.txt`).
- **Conclusion.** Knowledge is the lever (1.7% to about 30%). Once the definitions
  reach generation, the ontology performs like hand-supplied evidence (no
  significant difference either way). `TERM_DEFINITIONS_IN_GENERATION` is on by
  default since this measurement (PR #42 as merged).
- **Limits.** The change was designed after looking at why arm C failed on this
  same benchmark, so LiveSQLBench is **not held-out for this change** (PR #42
  says so). One run per question. The benchmark's KB is curated and complete; a
  customer's glossary will be neither. This tests loading and use of knowledge,
  not extracting it from documents (see section 5).
- **Where.** `reports-cprime/`, `pr33-check/fixed/armCprime.txt`; recorded in
  docs/evaluation.md ("Knowledge as Terms") and the register.

### E17 - Startup cost of re-ingestion (2026-11-12)

- **Finding.** Every container restart re-ran full ingestion for every registered
  database; one restart meant 20 databases and about 3,000 embedding rows, all paid
  gateway calls, to reproduce a stored result (PR #36 body; the figure is the PR
  author's, not re-counted).
- **Fix.** PR #36 skips databases whose catalog and embeddings are both present.
  Merged `FAKES`: the decision is unit-tested with stubs; the skip was observed
  once on the running stack after the next restart (notes: "startup pass: 0
  ingested, 20 skipped, 0 embeds"), but the register row still reads `FAKES`
  (the notes and the register differ; the register is the conservative record).

### E18 - Knowledge-use trace on real answers (2026-11-12 ~12:00)

- **Question.** Can an answer report which knowledge it was offered and used?
- **Setup.** PR #46; live check on the `alien_2` question of the frozen set.
- **Result.** The notes give "offered 11, used 6, undecided 3" for the final
  heuristic. The PR/register text says the first heuristic left 12 of 14 entries
  undecided, because KB definitions name columns in prose and column attributes
  were matched by label; the heuristic was then changed. The two figures are from
  different versions of the heuristic and the same question is assumed
  (**unverified**).
- **Limits.** `used` is a text heuristic on the final SQL, not a causal measure;
  it has not been compared with human judgement.

### E19 - A flaky connection-cap test (2026-11-12)

- **Finding.** `test_connections_are_bounded_while_used_and_released_after` failed
  once in CI (`assert 3 <= 2`). PR #47 body: 85 local loops (40 at test settings,
  20 forcing constant pool shrink, 25 with 24 CPU-burner processes on 12 cores)
  never reproduced it: 0 server-side peaks above 2, 0 client-side peaks above 2.
  The root cause (a closing backend still listed in `pg_stat_activity`) is
  **inferred from reading psycopg_pool, not observed**. The test now counts only
  `active` backends. No production code changed.

### E20 - Questions in French: does translating them help? (2026-11-12 ~13:30 onward)

- **Question.** Does a French question work as well as an English one on an
  English ontology, and does translating it first help? (Design decision 10 in
  [design/self-improving-ontology.md](./design/self-improving-ontology.md).)
- **Setup.** Chinook, target database `chinook`, 3 runs per question (1 for `en-v2`), 4 workers. English cases: the frozen `chinook.json` (cases hash
  4fbe2a00...). French cases: 20 French renderings `chinook_fr.json` (cases hash
  e628cfda...), reviewed by the user before any result; the file ships with
  PR #49 (open when this was written; not on `main`). Backend images built from
  the PR #49 branch; the first version had an on/off flag, the reworked version
  chooses the ontology language per database (`en` by default) and translates
  only when the question language differs. Scripts `lang_eval.sh`, `lang2_eval.sh`.
- **Results (raw runs verified from the JSON reports).**

| Arm | Questions passed (majority) | Runs passed |
|---|---|---|
| EN, translation off (`en-off`) | 20/20 | 60/60 |
| FR, translation off (`fr-off`) | 17/20 | 53/60 |
| FR, translation on (`fr-on`) | 20/20 | 59/60 |
| EN, translation on (`en-on`) | 20/20 | 59/60 |
| FR, reworked version (`fr-v2`) | 20/20 | 59/60 |
| EN, reworked version, 1 run (`en-v2`) | 18/20 | 18/20 |
| EN, reworked version, 3 runs (`en-v2b`) | 20/20 | 58/60 |

  Failures. `fr-off`: `acdc_tracks` wrong values 3 of 3; `best_customer` two runs
  and `rock_tracks` two runs cancelled at the 300 s run limit. `fr-on`: one
  `top5_countries_sales` run hit the 300 s limit. `fr-v2`: one `best_customer` run
  hit the limit. `en-on`: one `best_customer` run hit the limit. `en-v2`:
  `best_customer` hit the limit and `rock_tracks` returned wrong values (single
  run, 18/20). The 3-run rerun `en-v2b` (same cases hash) passed 20/20 questions,
  58/60 runs: one `best_customer` run hit the 300 s limit and one `acdc_tracks`
  run returned wrong values.
- **Statistics.** FR off vs FR on by majority: 3 questions only with translation,
  0 only without; exact two-sided McNemar p = 0.25 (computed for this document
  from those counts; not significant at 20 questions). The en-off to en-on
  difference (60 vs 59 runs) is one run and is noise.
- **Conclusion (cautious).** Without translation, a French question lost 3 of 20
  questions on an English ontology; with translation it matched English. English
  did not regress under the reworked version: the single-run 18/20 (`en-v2`) was
  run-to-run noise, as the 3-run rerun shows. `best_customer` hits the 300 s limit
  intermittently in every arm, English included, so that timeout is not caused by
  language; it is an open engine issue. The sample is 20 questions on one well-known
  database; the effect is suggestive, not established.
- **Limits.** The failure causes of `acdc_tracks` and `rock_tracks` were not
  investigated. Every error is a cancellation at the 300 s run limit of PR #40;
  whether load or reasoning caused the slow runs was not examined. `git_sha` is not recorded in these
  reports. The reworked arm used an image built from a worktree, not a released
  commit. Chinook is a set the engine does well on (E08).
- **Where.** `lexis-evals/lang/*.json` (maintainer machine); PR #49.

### E21 - Terms API on the running stack (2026-11-12)

- **Question.** Do the new create/rename/delete routes for Terms work against the
  real stack?
- **Result.** PR #45 then PR #48: `POST /api/terms` on Pagila (linked by name to
  2 tables, `embedded: true`, row present in `vdb.semantic_layer`), `PATCH` of the
  name re-embedded the row, `DELETE` removed the row and the Term, a second
  `DELETE` returned 404, one audit log line per action.
- **Limits.** A functional probe, not a measurement. Done without the web app's
  user header, so the 403 permission path was not exercised, and nothing went
  through the web app. `REAL-E2E`, manual.

### Built but never run

PR #44 (`bench split/run/score`) is tested with synthetic cases and fakes only;
it has not been run against the live stack or on LiveSQLBench. PR #25's
held-out value cases (`chinook_values.json`) and the clarification flow (PRs #26,
#27) have never been measured.

## 4. Summary of all measurements

| ID | Date | Set | Engine or arm | Result | Kind | Tag |
|---|---|---|---|---|---|---|
| E01 | 11-10 | Pagila 13x3 | empty index, loosened grader | 79.5% | invalid, retracted | REAL-E2E |
| E03 | 11-10 | Pagila 13x3, strict | `main` before #13/#15 | 61.5% | development | REAL-E2E |
| E03 | 11-10 | Pagila 13x3, strict | with #13 and #15 | 94.9% | development | REAL-E2E |
| E08 | 11-11 | Chinook 20x3 | pre-hardening 81f02eb2 | 70% | held-out | REAL-E2E |
| E08 | 11-11 | Chinook 20x3 | `main` d92eb4e7 | 100% | held-out | REAL-E2E |
| E09 | 11-11 | LSB 180x1, evidence | 81f02eb2 | 54/180 (30.0%) | held-out | REAL-E2E |
| E09 | 11-11 | LSB 180x1, evidence | `main` d92eb4e7 | 49/180 (27.2%); p = 0.40 vs 54 | held-out | REAL-E2E |
| E13 | 11-12 | LSB 180x1, evidence | `main` 02d166c0 (noise run) | 56/180 (31.1%); p = 0.17 vs 49 | held-out | REAL-E2E |
| E14 | 11-12 | LSB 180x1, no knowledge | arm A | 3/180 (1.7%) | LSB, held-out for this arm | REAL-E2E |
| E15 | 11-12 | LSB 180x1, KB as Terms | arm C | 4/180 (2.2%) | LSB | REAL-E2E |
| E16 | 11-12 | LSB 180x1, KB as Terms | arm C' (#42) | 55/180 (30.6%); vs C p < 1e-4; vs B p = 0.41, 1.0 | LSB, not held-out for #42 | REAL-E2E |
| E20 | 11-12 | Chinook EN 20x3 | translation off | 20/20, 60/60 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook FR 20x3 | translation off | 17/20, 53/60 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook FR 20x3 | translation on | 20/20, 59/60 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook EN 20x3 | translation on | 20/20, 59/60 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook FR 20x3 | reworked #49 | 20/20, 59/60 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook EN 20x1 | reworked #49 | 18/20 | held-out | REAL-E2E |
| E20 | 11-12 | Chinook EN 20x3 | reworked #49 | 20/20, 58/60 | held-out | REAL-E2E |

Engineering findings without a score: E02 (empty index), E04 (FK extraction), E05
(value probes and privacy incident), E06 (VIP term), E07 (learned test question),
E10 (connection exhaustion), E11 (host crash), E12 (429 storm), E17 (startup
re-ingestion), E18 (trace), E19 (flaky test), E21 (Terms API).

## 5. What is NOT yet measured

- **Accuracy gains on real, messy data.** Every score is on Pagila (development),
  Chinook (clean, well known) or LiveSQLBench (curated). Nothing is measured on a
  customer's own databases and questions.
- **Knowledge extraction from documents.** E16 starts from a curated KB. Whether
  Lexis can compile a prose wiki into correct Terms, and whether that beats plain
  retrieval over the same text, is planned (alien and archeology wiki texts, 20
  questions) and **not run**.
- **Value linking and clarification.** `chinook_values.json` and the clarification
  flow have never been measured. The "Indai" probe failed (E05).
- **Whether FK extraction (E04) changes accuracy.** Not measured.
- **Run-to-run variance beyond one pair.** One noise pair (E13); no multi-run
  confidence interval. 
- **Official-harness agreement.** `lsb-score` was checked against the frozen
  reports, synthetic data and a transcription, not against the benchmark's own
  harness on many tasks.
- **Concurrency limits as implemented.** PRs #35, #38, #40 are fake-tested; a
  live 429 burst, an orphaned-run kill and the slot cap under load are not
  re-measured end to end with the final code.
- **Human review** of the 20 Chinook expected answers and of the Pagila
  reference answers.
- **Self-improving behaviour** (learned Terms, promotion, consolidation) is a
  design only; no code or measurement.
- **Paraphrase robustness** of questions (memorisation check): not covered by the
  bench yet.
- **Other connectors** beyond Postgres in real use, and the MySQL/ClickHouse
  read-only checks beyond the one-off manual runs in the register.

## 6. Reproduction

**Environment.** A local stack from `docker-compose.yml` (backend :3001,
ingestion :3002, Postgres with the vector store, the web app), a model gateway
that is OpenAI-compatible, configured with the gateway's default chat model, an
embedding model and a rerank model (see [configuration.md](./configuration.md)).
Do not commit `.env` values. The semantic index must be populated for the target
database (`GET /api/semantic-compilation/status`); `run` refuses otherwise. Keep
the machine awake, use at most 8 concurrent chats and no more than about 12
concurrent gateway calls per IP. `--workers` above the backend's
`/api/health` cap is lowered by `bench`.

**Pagila (E03).** Load `dev_tools/fixtures/sql/pagila.sql`, ingest, compile, then
`python -m dev_tools.evals run --url http://localhost:3001 --label X --out X.json`
for each engine; `python -m dev_tools.evals compare main.json cand.json`. About
an hour for 13 x 3. Pagila is a development set.

**Chinook (E08, E20).** Load `dev_tools/fixtures/sql/chinook.sql` into a
Postgres database named `chinook` (the SQLite fixture must be converted: quoted
CamelCase names, 11 foreign keys), register and ingest it, compile, then
`python -m dev_tools.evals run --cases dev_tools/evals/cases/chinook.json
--target-db chinook --repeats 3 --workers 4 --label X --out X.json`. Verify
expected rows with `snapshot --check ... --dsn sqlite:///chinook.sqlite`. For the
French arms use `chinook_fr.json` from PR #49 and a backend built from that
branch. Scripts: `lang_eval.sh`, `lang2_eval.sh`.

**LiveSQLBench (E09, E13-E16).** Data must be obtained from the benchmark: the
public questions and knowledge-base files from
`huggingface.co/datasets/birdsql/livesqlbench-base-lite`, the database dumps
published by the benchmark, and the ground truth by e-mail request to the BIRD
team (see evaluation.md); keep all of it and every derived file outside the
repository. Then, as in evaluation.md: `lsb-build` (with `--oracle` for arm B;
without for arms A, C, C'), register and ingest the 18 databases, compile, run per
database (`python -m dev_tools.evals run --cases <db>.json --target-db <db>
--repeats 1 --workers 4`), `replay` infrastructure failures under one rule for all
arms, and `lsb-score --reports ... --cases ... --dsn-template ...` (official mode).
Arm C and C': `python -m dev_tools.evals lsb-kb --kb-dir <kb> --db <db> --apply`
first (undo with `--remove`), arm C' with `TERM_DEFINITIONS_IN_GENERATION=true`
(default since PR #42; set it to `false` to reproduce arm C). Run arm A before
loading any Terms. Check the frozen hashes before and after.
Scripts (maintainer machine, outside the repository): `nokb_run.sh`,
`noise_run.sh`, `kbterms_run.sh`, `cprime_run.sh`, helper `heavy.sh` (a lock that
serialises heavy commands). Scores: `lexis-evals/` and `livesqlbench/pr33-check/
fixed/` (`armA-vs-B.txt`, `noise.txt`, `armC.txt`, `armCprime.txt`, `official.txt`,
`clean.txt`). The raw reports are not in the repository, so E09-E16 are
**manual runs, not repeatable from the repository alone**; the repository gives
the tooling and the method.

**Repeatability warning.** Generation is not deterministic; expect about 4 points
of spread on a 180-question single run (E13) and use the paired test, not a bare
difference.
