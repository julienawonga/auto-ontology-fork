<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Design: a self-improving ontology for Lexis

Status: accepted with the maintainer's decisions of 2026-11-12 (section 10.3). No code changes accompany this document.
Date: 2026-11-12.

Source tags used throughout, inherited from the research synthesis
(`lexis-research/research-2026-11-12.md`, kept outside this repository):
**[E]** established (peer-reviewed paper, public benchmark, or official documentation
describing behaviour), **[V]** vendor claim or non-independent measurement,
**[I]** inference by the author of this design, **[B]** background knowledge not
re-verified. A decision that rests on a [V] or [I] source says so. Where a claim is
about Lexis itself, "today" means `main` at commit `dfbea133`.

## 1. Problem, goals, non-goals

### 1.1 Problem

Lexis answers business questions in SQL using a semantic layer (Terms, ColumnAttributes,
SqlAttributes, CustomAnalyses). Its own measurements say that layer is the dominant
lever: on 180 LiveSQLBench tasks the engine scores about 0% without the benchmark's
knowledge base and about 27-30% with it passed as evidence (`docs/evaluation.md`). The
literature points the same way: removing BIRD's evidence drops accuracy by more than
10 points ([DIVER, arXiv 2602.12064](https://arxiv.org/html/2602.12064), [E, preprint]),
and a 4 KB semantic document adds 17 to 23 points over schema alone on three frontier
models ([arXiv 2604.25149](https://arxiv.org/abs/2604.25149), [E, preprint, 100
questions]).

Today that knowledge is written by hand (or compiled from the schema) and never
improves with use. Every business term the engine does not know produces either a
guess or a clarification question whose answer lives and dies inside one conversation.
The roadmap items 3, 5 and 6 in `README.md` (metrics, learned terms, review and
promotion) describe the intent; this document specifies the mechanism and, more
importantly, how to measure it.

### 1.2 Goals

1. Knowledge learned from use (clarification answers, SQL corrections, accepted
   answers, documents, query history) becomes **draft** knowledge items with
   provenance, immediately useful to the person who produced it.
2. **Nothing learned reaches shared, certified knowledge without a human decision.**
3. Every item answers "who said this, when, from what evidence, with which model, and
   what has happened to it since", and the history is reconstructible.
4. Every claim of improvement is measured on frozen question sets that the learning
   pipeline cannot see.
5. Each phase ships alone and moves a number.

### 1.3 Non-goals

- Fine-tuning weights. All learning is in-context knowledge ([I], consistent with
  Reflexion, ExpeL and ACE: [Reflexion](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract.html)
  [E], [ACE, arXiv 2510.04618](https://arxiv.org/html/2510.04618v2) [E, authors' results]).
- Autonomous certification, ever. There is no "auto-approve above confidence X" mode.
- Replacing the compiled semantic layer. Compilation from the schema keeps working and
  keeps writing what it writes today.
- A new graph database or a general memory framework. The store stays Postgres with
  SQLAlchemy Core (`CLAUDE.md`).
- Row-level or per-user database permissions. That is a separate roadmap item; this
  design only governs knowledge, not data access.

### 1.4 Principles

| Principle | Consequence |
|---|---|
| The agent writes drafts only | The agent's identity has no permission to set `certified` anywhere. Enforced in the service layer and by a CHECK, not by prompt. |
| Scope is explicit: personal, team, shared | A draft is born personal. Only a human moves it outward. |
| Provenance on everything | No row without an actor, a source event and evidence pointers. A draft without evidence is rejected at write time. |
| Append-only history | State is a projection of events. Rollback is a new event. |
| Ranking is not truth | Usage and acceptance change retrieval order, never certification status (stigmergy without authority, [I]). |
| Measurable | Each mechanism names the metric that would show it helps, and the metric that would show it hurts. |

## 2. What exists and what is missing

Read from the code on `main`. This section is the baseline the rest builds on.

| Area | Exists today | Missing |
|---|---|---|
| Terms | `term` (`name`, `description`, `source` default `'semantic'`, `synonyms[]`, `name_certified`, `description_certified`, `owner_id`, `created`/`modified`/`created_by`/`modified_by`, `imported_id`; unique `(name, source)`) | Lifecycle beyond two booleans, scope, version history, evidence, confidence, validity period |
| ColumnAttribute | `column_attribute` with `source`, `certified`, `description_suggestion`, a 5-part merge key | Same as above |
| SqlAttribute | `sql_attribute` with `expression`, `source` in (`manual`,`sql`,`table`,`bridgeTable`), `certified`, `owner_id`, `description_suggestion`; **`name` globally unique**; one Term per attribute via `sql_attribute__term` | Per-user drafts (global name uniqueness would collide), schema-version stamp, execution validation record, provenance of the proposer |
| Query history | `sql_query` with `total_counter`, `last_seen`, structural fingerprint; `sql_query__table`, `sql_query__column` | A link from a query to the user question that produced it |
| Suggester | `semantic/sql_attribute_suggester.py`: extracts expressions from history, scores by 3-month usage, an LLM picks the top 10 per Term, writes attributes. Behind `sql_attribute_suggestion_enabled`, **off by default** (PR #31) because the output was unreviewed and had learned a threshold typed in a test question (`docs/verification-status.md`) | A review state to write into. Until it exists the stage must stay off. |
| Clarification | `retrieval/text_to_sql/clarification.py` decides when to ask (rules err toward answering); `server/chat/clarification.py` stores the question as a fenced block in the message, treats the conversation as the only state, and turns the answer into `The user defined "<term>" as: <answer>` passed as `evidence` for that turn only | Persistence beyond the conversation. The answer is lost to every other user and every later session. |
| Feedback | None in the backend: no thumbs or edit-capture endpoint | Thumbs, "this SQL was wrong", edit-and-rerun capture |
| Retrieval | `search_semantic_index` over the pgvector store (`auto_ontology/vdb`), per-label top-k, metadata filter by database and label; labels include Term, ColumnAttribute, SqlAttribute | Scope and status in the filter; ranking by certification; dependency-chain retrieval |
| Identity | `x-auto-ontology-user-id` header forwarded by the gateway; `owner_id` on term and sql_attribute; `user_group` tables; internal auth token (PR #37) | A reviewer role on knowledge; per-domain review ownership |
| Eval | `dev_tools/evals`: result-set scoring (never SQL text), repeated runs, `compare`, `replay`, Pagila dev set, Chinook frozen held-out, LiveSQLBench loader with oracle KB and paired test; `dev_tools/integrity` freezes case files by hash and scans shipped code for eval answers | A learning-over-time harness, a canary slice, a regression-on-promotion gate |
| Verification register | `docs/verification-status.md`, CI-checked | Rows for every new capability (a rule of this repo, repeated in the phases) |

Two existing facts shape the design:

- `sql_attribute.name` is globally unique and `term (name, source)` is unique. Personal
  and conflicting drafts cannot live in those tables without breaking the constraint.
  Therefore drafts live in new tables, and certified items are **materialised** into the
  existing tables, which keeps compilation, the index builders, the glossary page and
  model interchange working unchanged.
- The integrity scanner covers `auto_ontology/`, `frontend/` and `mcp/`, not `docs/`. It
  stays useful here: nothing learned at runtime is committed to shipped code, which is
  the property that matters for contamination (section 8.4).

## 3. Knowledge item model

### 3.1 Item kinds

A knowledge item is a proposal to create or change one of:

| Kind | Materialises into | Example |
|---|---|---|
| `term` | `term` row | "VIP customer": definition and synonyms |
| `synonym` | element of `term.synonyms` and a `term_label` row (3.4) | "chiffre d'affaires" is a label of "revenue" |
| `column_attribute` | `column_attribute` | revenue is `payment.amount` |
| `sql_attribute` | `sql_attribute` + `sql_attribute__term` + `sql_attribute__sql` | `amount > 0 AND status = 'paid'` named "Paid revenue" |
| `verified_query` | new `verified_query` table (phase P2) | a certified question and its parameterised SQL |
| `relation` | a dependency between items, or a Term `related` link | "Churned customer" is defined in terms of "Active customer" |

`verified_query` is new because today a CustomAnalysis is a name plus SQL with no
question text, no parameters and no verification stamp. Databricks marks an answer
"trusted" only when the stored SQL is reused as written ([trusted assets](https://docs.databricks.com/aws/en/genie/trusted-assets), [E]);
Snowflake stores `verified_by` and `verified_at` ([VQR](https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-repository), [E]).
CustomAnalysis is kept; a certified CustomAnalysis can be promoted into a
verified_query by adding the question and parameters.

### 3.2 Lifecycle

```
                 propose (agent or human)
                          |
                          v
                        draft  (scope=personal; usable only by its owner)
                          | submit
                          v
                      in_review
              /           |            \
          reject       approve      request_changes --> draft
             |            |
             v            v
         rejected    canary gate (6.2)
                          | pass
                          v
                      certified (scope = team | shared)
              /           |            \
  schema or dependency    |      contradicted by another
  change, validity ends   |      live item
            v             |             v
   needs_revalidation     |        conflicted
            |             |             |
   revalidate_ok -> certified      human resolves
            |                           |
            +-------> deprecated <------+
```

| Status | Meaning | Who or what sets it |
|---|---|---|
| `draft` | Proposed, not submitted. Personal scope. | agent or user |
| `in_review` | Submitted; sits in a reviewer's queue. | owner of the draft |
| `certified` | A human approved it and it passed the canary gate. Materialised into the live tables. | reviewer (human only) |
| `needs_revalidation` | A certified item whose evidence may no longer hold (schema change, dependency changed, validity period elapsed). Still served, but not badged and flagged in the answer. | consolidation job |
| `conflicted` | Two live items disagree (6.2). Both are served with a warning, neither is badged. | consolidation job |
| `deprecated` | Superseded or withdrawn. Kept for audit, not retrieved. | reviewer |
| `rejected` | A reviewer refused it. Kept so the same proposal is not re-queued; the reason is evidence for the suppressor (7.1). | reviewer |

Transitions are enforced by one function that takes an actor, an action and a target,
and refuses anything not in a transition table. `certified` can only be entered by an
action carrying a human `actor_id` whose role passes the permission check (7.2).

### 3.3 Scope

| Scope | Who can read it at answer time | Who can create | Who can widen |
|---|---|---|---|
| `personal` | the owner only | the owner, or the agent acting for the owner | the owner may submit |
| `team` | members of a `user_group` | reviewer of that group | reviewer |
| `shared` | everyone with access to the database | reviewer with shared-review right | reviewer |

Scope is orthogonal to status except for one invariant: `certified` implies scope in
(`team`, `shared`). A personal item cannot be certified; submitting it copies it to the
target scope as a new version under review. This keeps the audit story simple: a
certified item always has a reviewer in its history. Isolation by scope is also the first
poisoning defence: a user who plants a bad definition affects only their own answers
(6.3, [I], built on the MINJA threat model).

### 3.4 Concrete tables

Names are proposals. All are declared in `auto_ontology/dal/schema.py` and added by
Alembic (`uv run alembic revision --autogenerate`), nothing hand-edited.

**`knowledge_item`** (the current projection, one row per item)

| Column | Type | Note |
|---|---|---|
| `id` | text pk | `_id()` helper like the rest |
| `kind` | text, CHECK in the kinds above | |
| `status` | text, CHECK in the statuses above | |
| `scope` | text, CHECK in (`personal`,`team`,`shared`) | |
| `scope_owner_id` | text | account id (personal) or `user_group.id` (team), null for shared; not an FK, for the reason given on `owner_id` |
| `database_id` | text null | items are per database; null only for a global glossary |
| `target_table`, `target_id` | text null | the live row it was materialised into (`term.id`, `sql_attribute.id`) once certified; null for drafts |
| `supersedes_id` | text null, self FK | the item this one replaces or amends |
| `current_version` | int | pointer into `knowledge_item_version` |
| `content_hash` | text | normalised content; used for dedup (6.1) |
| `confidence` | real null | 0-1, section 5.3; never used for status |
| `evidence_level` | smallint | 1 reviewer-certified and executed, 2 executed and accepted by several users, 3 unreviewed agent proposal |
| `usage_weight` | real | decayed counter, ranking only |
| `schema_fingerprint` | text null | hash of the catalog slice the item depends on, at last validation |
| `last_validated_at` | timestamptz null | |
| `valid_from`, `valid_to` | timestamptz null | business-rule drift; "as of" reads (6.6) |
| `created`, `modified` | timestamptz | the `_edited()` helper |

**`knowledge_item_version`** (immutable content snapshots)

`item_id`, `version`, `content` JSONB (definition, `pref_label`, `alt_labels[]`,
`expression`, `parameters[]`, `dependencies[]` as item ids), `content_hash`,
`created_at`, `created_by`, and the `event_id` that produced it. Primary key
`(item_id, version)`. JSONB is deliberate: the shape differs by `kind`, and the fields
that must be queried (status, scope, hash, owner) are real columns on `knowledge_item`.

**`knowledge_event`** (append-only, the source of truth)

`id`, `item_id`, `at`, `action` (`propose`, `edit`, `submit`, `approve`, `reject`,
`request_changes`, `deprecate`, `merge`, `rollback`, `mark_conflict`,
`mark_revalidation`, `revalidate_ok`, `promote_scope`), `actor_type`
(`user` | `agent` | `job`), `actor_id`, `from_status`, `to_status`, `version`,
`comment`, and PROV-like fields:

| PROV-O concept ([W3C PROV-O](https://www.w3.org/TR/prov-o/), [B]) | Column |
|---|---|
| Agent (attributed to) | `actor_type`, `actor_id`, plus `agent_model` and `prompt_version` when `actor_type='agent'` |
| Activity (generated by) | `activity` (`clarification_answer`, `sql_edit`, `thumbs`, `document_import`, `query_history`, `consolidation`, `human_review`), `job_run_id` |
| Entity (derived from) | rows in `knowledge_evidence` |

There is no UPDATE or DELETE path to this table in the service layer; a database role
without UPDATE and DELETE grants (or a trigger) makes that a property of the store rather
than a convention [I]. The projection is rebuilt by replaying events, and a test asserts
that replay reproduces the stored projection.

**`knowledge_evidence`** (what supports an event or version)

`id`, `event_id`, `evidence_type` (`conversation_message`, `episode`, `sql_query`,
`document_span`, `clarification`, `execution_result`), `ref` (id or locator), `excerpt`
(bounded, sanitised text, see 6.3), `captured_at`. An item must have at least one
evidence row at propose time; the service raises otherwise.

**`episode`** (fast, episodic memory; the "hippocampus" half of Complementary Learning Systems, [Kumaran et al. 2016](https://web.stanford.edu/~jlmcc/papers/KumaranHassabisMcClelland16FinalMS.pdf) [E])

`id`, `user_id`, `conversation_id`, `message_id`, `question`, `question_hash`,
`retrieved` JSONB (item ids and versions that were in the prompt), `candidate_sqls`
JSONB, `final_sql`, `exec_status`, `result_fingerprint`, `confidence_trace` JSONB,
`route` (`fast` | `slow`), `clarification_id` null, `outcome` (`accepted`, `edited`,
`rejected`, `unrated`), `schema_fingerprint`, `ts`. Retention is bounded (proposal: 90
days, configurable); evidence excerpts copied into `knowledge_evidence` survive. This is
the table the replay harness and the consolidation job read.

**`knowledge_use`** (what an answer used; backs the "knowledge used" panel)

`episode_id`, `item_id`, `item_version`, `role` (`evidence`, `sql_reused_verbatim`,
`retrieved_unused`), `rank`. Only `role='sql_reused_verbatim'` can produce the Verified
badge (5.2).

**`term_label`** (SKOS-like labels, [SKOS](https://www.w3.org/TR/skos-reference/) [B])

`term_id`, `label`, `kind` (`pref`, `alt`, `hidden`), `item_id` (the knowledge
item that introduced it). It replaces nothing: `term.synonyms` stays as the array the
rest of the code reads and is written from certified labels at materialisation. It is
needed because each synonym must carry its own provenance and be revocable
individually. There is no `lang` column: the ontology is English only (decision 10,
section 10.3); questions in other languages are handled at answer time.

### 3.5 How it maps onto the existing tables

| Existing | Treatment |
|---|---|
| `term.name_certified`, `description_certified`, `sql_attribute.certified`, `column_attribute.certified` | Kept as the **materialised fact** the rest of the app reads. Set only by the certify transition, in the same transaction that writes the event. Rows certified by hand before this feature get a synthetic `approve` event (`actor_type='user'`, `activity='legacy_certification'`) in a backfill, so history is complete. |
| `term.source`, `sql_attribute.source` | Unchanged. New value `'learned'` for rows born from a promotion (the `sql_attribute.source` CHECK allows `manual`, `sql`, `table`, `bridgeTable` today; the migration extends it). `'sql'` is what the suggester writes now. |
| `created_by`, `modified_by` | Unchanged: they record the human who last saved in the UI, and null reads as Auto Generated. Agent authorship lives in `knowledge_event`, so these columns keep their meaning. |
| `owner_id` | Becomes the default **reviewer** for items touching that row; glossary-page semantics unchanged. |
| `description_suggestion` | Kept. It is a single slot; the new model generalises it. Existing non-null suggestions become `draft` items (`activity='compilation'`); the column is deprecated, not dropped. |
| `custom_analysis` | Unchanged in P0-P1; a source of `verified_query` in P2. |
| `sql_query` | Gains a nullable link table `episode__sql_query`, so a stored statement can point to the question that produced it. |

Migration sketch, one revision per phase, never edited once applied:

1. P0 revision: create `knowledge_item`, `knowledge_item_version`, `knowledge_event`,
   `knowledge_evidence`, `knowledge_use`, `episode`, `term_label`; extend the
   `sql_attribute.source` CHECK with `'learned'`; backfill legacy-certification events for
   rows where any certified flag is true; backfill `description_suggestion` drafts. All new
   columns are nullable or defaulted, and downgrade is a pure DROP.
2. P2 revision: `verified_query`, `verified_query__param`.
3. P3 revision: `knowledge_review_assignment`, `knowledge_quota`.

No existing column is dropped or retyped, so a deployment can run the P0 schema with the
feature flags off and behave exactly as today.

### 3.6 The semantic index

The vector store (`vdb`; labels Term, ColumnAttribute, SqlAttribute; metadata `database`
and `label`) indexes live rows. Design choices:

- Certified items reach the shared index only through materialisation, so compile and
  rebuild paths (`index_health`, `embed`) keep working unchanged.
- Drafts are embedded under a separate label set (`DraftTerm`, `DraftSqlAttribute`) with
  metadata `scope`, `scope_owner_id`, `status`. The metadata filter is built server-side
  from the caller's identity and cannot be set by the client, so a personal draft is never
  returned to another user, even on a similarity match.
- Delta updates: a status change or edit re-embeds one item, not the database. The index
  build writes per node already; P1 adds a single-item path [I].

## 4. Learning signals and capture

Principle: **the agent proposes from evidence; it never asserts.** Each path below
produces a `draft` in the actor's personal scope, a `knowledge_evidence` row and an
`episode` row. Signals ordered by strength ([I], informed by Genie using thumbs and query
downloads as signals, [E](https://docs.databricks.com/en/genie/knowledge-store)):

| Signal | Strength | Becomes | Evidence stored | Safeguards |
|---|---|---|---|---|
| Reviewer approves a draft | authority | certified | the review event | human only |
| Clarification answer | strong for definitions and synonyms | `term` draft (definition), `synonym` draft (the asked phrase as a label of the chosen term) | the clarification block (question, options offered, option chosen or free text), message ids | free text is untrusted input (6.3); option choices preferred |
| User edits the SQL and re-runs it successfully | strong for attributes | `sql_attribute` or `column_attribute` draft derived from the diff between generated and final SQL | the question, both SQLs, the result fingerprint | the diff is computed with the parser already in use (sqlglot), not by the LLM; the LLM only names and describes it |
| Question repeated by several users with consistent accepted SQL | medium | `verified_query` candidate (P2) | the episodes (distinct users, count, agreeing result fingerprints) | at least N distinct users; same-user repeats count once |
| Thumbs up | weak | raises `usage_weight` and sets the episode outcome; alone never creates a draft | episode | ignores bursts from one user |
| Thumbs down with a comment | medium | flags the used items for review; may open a conflict check; with a corrected SQL it becomes an edit signal | episode, comment | the comment is untrusted text |
| Document or KB import (glossary, data dictionary, a LiveSQLBench-style KB) | depends on source | `term` and `sql_attribute` drafts in batch, `evidence_type='document_span'` | file hash, span, extractor model and prompt version | one bundle in the review queue; an importer never writes `certified` |
| Query history (the existing suggester) | weak to medium | `sql_attribute` drafts, `activity='query_history'` | statement ids, scores | stays off until the draft pipeline exists; then writes drafts only, `evidence_level=3` |

### 4.1 Clarification answers become drafts

Today `definition_evidence` turns the answer into evidence for one turn. In P1 the same
event also calls `propose`:

1. The answer is parsed (`option_id` or `text`) as now.
2. If an option was chosen, the draft is a `synonym` or `term` definition whose content is
   that option's label and description; the offered options and the chosen one are
   stored as evidence. If free text was typed, the draft gets `evidence_level=3`, a
   `free_text` flag, and the text passes through the sanitiser (6.3) before storage.
3. The draft is personal. The same turn already uses it through `evidence`; from the next
   turn it is retrieved as a personal draft (5.1), so the user is not asked again. The UI
   says "Saved as your draft definition of 'VIP'. Submit for review."

This reuses the existing mechanism and keeps its property that the conversation is the
state for the pending question.

### 4.2 SQL edits

Lexis has no SQL-edit flow today. P1 adds an "edit and run" action on the SQL block. The
edited statement passes through the same read-only guard as agent SQL
(`docs/query-safety.md`); a rejected statement is never stored as evidence of anything.
The diff is classified deterministically (added predicate, changed aggregation, changed
join, changed column) and only a small set of classes produce a `sql_attribute` draft;
the rest are recorded in the episode and surfaced in the consolidation report. This is
the double-loop step ([Argyris, B], [I]): many edits of one class for one Term raise a
proposal to change the **Term definition**, not only to add a snippet.

### 4.3 Repeated questions and accepted answers

An episode counts as accepted when the user gave a thumbs up, copied or exported the
result, or asked a follow-up that builds on it without correcting it. Acceptance is weak
alone and is used for ranking (`usage_weight`) and for the P2 verified-query candidates,
which require several distinct users and agreeing result fingerprints. This guards
against the failure already seen with the suggester (PR #31).

### 4.4 Documents

A document is a separate producer that must not bypass review. A KB file becomes a bundle
of drafts with `evidence_type='document_span'`. It is also the path for the immediate
LiveSQLBench experiment (8.6).

## 5. Use at answer time

### 5.1 Retrieval preference order

For each question the retriever builds a candidate pool, then ranks and budgets it:

| Tier | Source | Prompt label | Badge eligible |
|---|---|---|---|
| 1 | status `certified`, shared or team | "Certified" | yes (SqlAttribute or verified query reused unchanged) |
| 2 | certified but `needs_revalidation` or `conflicted` | "Certified, under review" / "Disputed" | no |
| 3 | the asker's personal drafts | "Your draft" | no |
| 4 | other visible drafts (team items in review, if the asker is in that team) | "Proposed" | no |

Within a tier: embedding similarity, then `usage_weight`, then recency of validation (a
composite in the spirit of generative-agent memory retrieval, [B]). Never an item from
another user's personal scope. A hard budget caps knowledge in the prompt (proposal: 12
items, at most 3 verified-query examples; Fabric retrieves the 3 most relevant examples,
[E](https://learn.microsoft.com/fabric/data-science/data-agent-configurations), and
Genie caps spaces at 100 instructions and 200 snippets,
[E](https://docs.databricks.com/en/genie/knowledge-store)). Dependency chains come in
whole: when "Churned customer" depends on "Active customer", both are retrieved or
neither, because LiveSQLBench's KB is hierarchical and multi-hop
([E](https://github.com/bird-bench/livesqlbench)).

When two items of one Term disagree, both are included, labelled disputed, and the model is
told to ask rather than pick (5.4).

### 5.2 How drafts are labelled in answers

The prompt carries the tier label per item. The answer metadata (not the model's prose,
which can drop it) carries `answer_provenance`:

| Value | Condition |
|---|---|
| `verified_reuse` | A certified, non-stale SqlAttribute or verified query was used with its SQL unchanged (parameters substituted only). Checked in code by structural comparison of the final SQL against the stored SQL after parameter binding. |
| `knowledge_guided` | Certified knowledge was in the prompt; SQL was generated. |
| `draft_guided` | At least one draft was used. The UI says "Based on your unreviewed definition of X". |
| `free_generation` | No retrieved knowledge applied. |

The **Verified badge is shown only for `verified_reuse`.** This follows the rule Databricks
documents for Trusted ([E](https://docs.databricks.com/aws/en/genie/trusted-assets)). A
badge for "knowledge was in the prompt" would overstate what is known. That users read the
badge as a correctness claim is [I]; 8.1 measures the accuracy within badged answers
instead of assuming it.

### 5.3 "Knowledge used" panel and confidence

Every answer lists the items from `knowledge_use` with status, scope and age, as Fabric's
Run Steps and Snowflake's `confidence` field do
([E](https://learn.microsoft.com/fabric/data-science/data-agent-configurations),
[E](https://docs.snowflake.com/en/user-guide/views-semantic/verified-query-repository)).
Each item has "wrong" and "right" actions that feed section 4.

Answer confidence is built from signals the system can compute, not from the model's
self-report (self-checks by prompt do worse than calibrated probabilities or
execution-based confidence, [arXiv 2508.14056](https://arxiv.org/html/2508.14056v1)
[E, preprint]):

- agreement among N generated candidates by execution result (ReFoRCE and CHASE-SQL use
  agreement or pairwise selection, [ReFoRCE](https://arxiv.org/pdf/2502.00675),
  [CHASE-SQL](https://arxiv.org/abs/2410.01943), [E]);
- term coverage: every retrieved Term the question mentions appears in the SQL (a
  post-generation check, [I]);
- the tier of the best matching knowledge (certified verbatim > certified guided > draft > none);
- execution success, empty-result flag, row-cap hit.

The combiner starts as a transparent rule table, not a learned model, and is calibrated
against reviewer-labelled outcomes once there are enough (8.1 names ECE). Until
calibrated, the number is not shown to users, only the route and the badge.

### 5.4 Fast path and slow path (System 1 / System 2)

Routing by uncertainty follows the dual-process idea ([Kahneman, B]; LLM instances:
[PRIME](https://arxiv.org/pdf/2509.22315v2), [E, preprint]). The mapping onto Lexis is [I].

| | Fast path | Slow path |
|---|---|---|
| Trigger | A certified item matches with high similarity and its SQL applies by parameter substitution; or retrieval returns tier-1 knowledge for every mentioned term | A mentioned term without certified coverage; disputed items; low candidate agreement; empty result |
| Work | One retrieval, one generation (or direct verified reuse, no generation) | Multi-candidate generation, term coverage check, column exploration, execution-guided repair |
| Cost | Lowest latency and tokens | Several model calls |
| Output | Answer, possibly Verified | Answer with provenance, or a clarification |

When to ask. BIRD-Interact reports GPT-5 solving 8.67% (conversational) and 17.00%
(agentic) of tasks with user simulation and points at communication strategy as the weak
spot ([arXiv 2510.05318](https://arxiv.org/pdf/2510.05318), [E]; the attribution to
strategy comes from a secondary summary, so verify it in the paper). The rule is
therefore conservative and measurable: ask only when (a) a mentioned term has no
certified coverage, **and** (b) candidates disagree on the term's reading, **and** (c) the
user has not already answered this phrase (no personal draft exists). The existing
detector's bias towards answering is kept; the new input is the knowledge status. Success
metrics: of clarifications asked, the share whose answer changed the SQL result; and the
rate of user corrections after confident answers (8.1). Asking too often is a measured
regression, not a feeling.

## 6. Consolidation job ("sleep")

An asynchronous job, not in the request path. It is the slow learner of the
Complementary Learning Systems account ([E](https://web.stanford.edu/~jlmcc/papers/KumaranHassabisMcClelland16FinalMS.pdf)):
episodes arrive fast, structure is extracted offline. Letta's sleep-time compute
([V, vendor blog](https://letta.com/blog/sleep-time-compute)) and ACE's
generate-reflect-curate loop with delta updates, designed to avoid the context collapse
that repeated rewrites cause ([E, authors' results](https://arxiv.org/html/2510.04618v2)),
motivate the shape. Rule taken from ACE: **the job emits delta events and never rewrites
an existing item**; an edit is a new version with a diff.

| Step | What it does | Output |
|---|---|---|
| 6.1 Dedup | Group drafts by `content_hash`, and by embedding similarity plus overlap of referenced columns; propose a `merge` for near duplicates | `merge` proposals, never automatic, bulk-reviewable |
| 6.2 Conflict detection | For each Term, compare live definitions and SqlAttribute expressions: same label, different expression or contradicting definition (parser comparison of predicates where possible, an LLM judge for prose) | `mark_conflict` events, both items flagged, a review task |
| 6.3 Revalidation | On each compilation, compare the catalog slice against `schema_fingerprint`. Items whose referenced tables or columns changed or vanished go to `needs_revalidation`. The job parses and `EXPLAIN`s the SQL against the new schema without executing it, and where allowed runs it with the row cap | `revalidate_ok`, or a draft fix proposal |
| 6.4 Usage weighting and decay | `usage_weight = sum(accepted uses * exp(-age/tau))`; ranking only. Items unused for a long time leave the index but are not deleted or deprecated | ranking and index hygiene |
| 6.5 Candidate generation | Group episodes by Term and column; surface repeated edit classes (4.2), repeated clarifications, and the highest-surprise episodes (confident answer, user corrected) first, because errors carry the most information ([predictive coding, B, I]) | draft proposals; the weekly queue |
| 6.6 Business-rule drift | A changed definition sets `valid_to` on the old version and `valid_from` on the new; dependents move to `needs_revalidation`, cascading through `dependencies` | cascade and a drift report |

Revalidation needs a notion of "what changed", which ingestion does not emit today. The
fingerprint can be computed from the `catalog_*` tables at compile time, so the job
compares fingerprints without changing ingestion [I].

### 6.1 Weekly queue and quotas

The job writes a ranked queue. Ranking is expected impact:
`uses_in_window * surprise / (1 + existing_certified_coverage)`. The review UI shows the
top N (proposal 10, configurable; Snowflake caps its suggestions at 10,
[E](https://docs.snowflake.com/user-guide/views-semantic/verified-query-suggestions); the
number is a review-fatigue choice, [I]).

### 6.2 The canary gate

Before `approve` takes effect, the candidate is applied in a staging retrieval overlay and
the **canary slice** (8.2) is replayed. Promotion proceeds only if the canary is not worse
than the stored baseline by more than a configured tolerance. A promotion that no canary
question touches is allowed but marked, and counted in an "uncovered promotions" metric,
so the gap is visible rather than hidden [I]. Shadow mode (an item evaluated on live
traffic without being served) is the P3 extension.

### 6.3 Safety

| Risk | Mechanism |
|---|---|
| Poisoning through memory writes: [MINJA](https://arxiv.org/abs/2503.03704) injects malicious records through ordinary queries (98.2% injection success and 76.8% attack success, per the authors, [E, preprint]) | (1) Nothing learned reaches shared knowledge without human review; (2) personal scope confines a poisoned draft to its author; (3) provenance on every item; (4) free text and document text are untrusted data, wrapped as such in any prompt and never concatenated into instructions; (5) anomaly checks on submissions: bursts per user, definitions that contradict certified items, SQL with side-effect functions, items whose evidence is a single message; (6) per-user and per-item quotas on drafts and submissions; (7) the canary gate. The defences evaluated in [arXiv 2601.05504](https://arxiv.org/html/2601.05504) [E, preprint] indicate what to test; they are not a guarantee. |
| Prompt injection through a clarification answer or a document | Excerpts are length-capped and stripped of control sequences; stored text is rendered only inside delimited data blocks; a set of injection strings is part of the unit tests and of the canary |
| A bad promotion silently regresses accuracy | The canary gate (6.2) |
| Reviewer fatigue turns approval into a rubber stamp | Bounded queue, diff view, and an audit of each reviewer's approve rate and median time per item; approving everything in seconds is itself flagged |
| Runaway cost | A token budget per job run; the cheaper configured model for consolidation; a cap on LLM calls per run |

## 7. Review UX

### 7.1 Surfaces

| Surface | Content |
|---|---|
| Queue | Items in `in_review`, `conflicted`, `needs_revalidation`, sorted by expected impact; filters by Term, database, source activity, proposer; counters |
| Item page | Current vs proposed content as a diff; provenance (who, when, model, prompt version); each evidence row with its excerpt and a link to the conversation if the viewer may see it; usage and outcomes (retrieved, accepted, corrected); the canary result; dependent items |
| Actions | Certify; Reject (reason required, feeds the suppressor so the same proposal is not re-queued); Request changes; Edit then certify (the edit is a new human-authored version); Merge duplicate; Deprecate |
| Bulk | Select by activity and Term; approve or reject a document bundle or a set of identical synonyms in one action, each still producing one event per item |
| Audit log | The `knowledge_event` stream, filterable by actor, item, action and date; CSV export; read-only by construction |
| Personal view | "My drafts": submit, withdraw, delete (a `deprecate` event on a personal draft, so the trail is preserved) |

The glossary page already shows a Term's owner, certified flags and descriptions; the item
page extends it rather than adding a second place to look.

### 7.2 Who may certify

Permissions follow the repo's identity model (gateway user id, `user_group`, `owner_id`).
Proposed rules, listed as decisions in 10.2:

| Action | Personal | Team | Shared |
|---|---|---|---|
| Propose or edit own draft | owner | n/a | n/a |
| Submit for review | owner | n/a | n/a |
| Certify | n/a | group member with reviewer right | domain owner (`owner_id` of the Term's tables) or admin |
| Second reviewer | n/a | optional | required for Terms tagged high-impact (finance, compliance), like a second opinion in clinical decision support ([B], [I]) |
| Certify one's own proposal | never | never | never (enforced in the transition function) |

The model is OpenMetadata's glossary approval: only reviewers act on a Task, and editing
an approved term sends it back to review
([E](https://docs.open-metadata.org/latest/how-to-guides/data-governance/glossary/approval)).
A human edit to a certified item creates a new version in `in_review`. The consolidation
job never edits a certified item and never overwrites a human edit (README roadmap item 8).

## 8. Evaluation plan

The product claim is "the ontology improves with use without regressing". Every part of it
is tested on sets the learning pipeline cannot touch.

### 8.1 Primary and guardrail metrics

| Metric | Definition | Why |
|---|---|---|
| **Certified term coverage** (primary) | Over a frozen question set, the share of business terms in the questions that resolve to a `certified` item at answer time (`knowledge_use` plus a reference list of terms per question) | A semantic layer cannot answer what it does not model (dbt's own benchmark of 11 questions found 3 needing too many hops, [V](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026)); coverage as the leading indicator is [I] |
| **Answer accuracy** (primary) | Result-set match on the golden set, `dev_tools/evals` scoring (never SQL text), repeated runs, paired comparison | Already implemented and strict |
| Verified reuse rate | Share of answers with `verified_reuse` | Cost and trust |
| Accuracy of Verified answers | Accuracy within `verified_reuse` | Must be near 1, otherwise the badge misleads |
| Proposal precision | Share of agent drafts a reviewer approves unchanged or with light edits | Extractor quality and reviewer load |
| Review latency, queue depth | Median time to decision | Governance health |
| ECE and AUROC of confidence | Against reviewer or golden labels (the 0.82 AUROC of a two-judge ensemble in [arXiv 2607.06799](https://arxiv.org/abs/2607.06799) is a reference point, [E, preprint], not a target) | Metacognition |
| Clarification utility | Share of asked clarifications whose answer changed the result; share of wrong-but-confident answers | Ask only when it pays |
| Guardrails | Held-out accuracy must not drop; clarification rate, user-correction rate, latency and tokens per answer must stay inside a tolerance stated before the run | A phase is not done if a guardrail moves the wrong way |

### 8.2 Frozen sets

Three slices of the golden set per database, 2-4 paraphrases per question (Genie's
recommendation, [E](https://www.databricks.com/blog/building-confidence-your-genie-space-benchmarks-and-ask-review)).

| Slice | Use | Who sees it |
|---|---|---|
| Dev | tuning prompts, thresholds, extractors | engineers and the pipeline |
| Held-out | reported numbers and learning curves | never the pipeline or prompts; frozen by hash with `python -m dev_tools.integrity freeze`, as `chinook.json` is |
| Canary | replayed before every promotion; small, fast, covering the most-used Terms | the promotion gate only |

LiveSQLBench's rule that a hidden split becomes the next version's dev split
([E](https://github.com/bird-bench/livesqlbench)) is adopted: once a held-out set has
informed several decisions, retire it, freeze a new one and record the date in
`docs/evaluation.md`.

### 8.3 Learning curves over a replayed question stream

1. Build a dev stream: an ordered list of questions with expected answers and, for some,
   a scripted user behaviour (answer this clarification with option k; edit to this SQL).
   Scripted users are the weakness: BIRD-Interact's user simulator is the precedent
   ([E](https://arxiv.org/pdf/2510.05318)), and a simulator is a confound, so a small
   run with real humans is required before any product claim.
2. Run the stream in order through a fresh instance with learning on. Drafts are
   submitted and certified **only by a scripted reviewer applying a fixed rule against the
   stream's ground truth**. This tests the mechanism, not reviewer quality, and the report
   says so.
3. After every K questions, score the held-out slice. Plot held-out accuracy and certified
   coverage against questions seen. Arms: learning off; learning on, personal scope only;
   learning on with promotion.
4. Transfer: the held-out slice holds paraphrases and sibling questions of the stream.
   Forgetting: the stream's earlier questions are re-asked at the end, and the drop is the
   regression metric. Drift: change a definition mid-stream (the idea of LiveSQLBench-Large's
   Business Rule Drift, [E](https://github.com/bird-bench/livesqlbench)) and report time to
   adopt it and collateral failures.

`dev_tools/evals` has `run`, `compare` and `replay` but no stream mode. P0 adds one that
resets state between arms, writes one report per checkpoint and reuses the existing
scoring and paired test.

### 8.4 Contamination rules

These extend "Rules for an honest measurement" in `docs/evaluation.md`:

1. A golden-set question, its paraphrases and its expected rows never enter a knowledge
   item as example, evidence or `verified_query`. The pipeline hashes normalised question
   text and refuses to write an item whose evidence matches a frozen set (the token-sequence
   idea of `dev_tools/integrity/scan.py`, applied at runtime to the store, [I]).
2. The held-out slice is never read by an extractor, a prompt author or the consolidation
   job. The canary is read only by the promotion gate.
3. Knowledge written during an evaluation run goes to a scratch database and is discarded;
   the harness refuses a target that is a production store.
4. A reported number names the frozen file hash, the commit, the model, the number of runs
   and the paired-test result. No threshold or prompt changes after a held-out result is
   seen; if one does, the slice is retired.
5. Genie separates benchmarks (not injected) from instructions (injected) for the same
   reason ([E](https://docs.databricks.com/aws/en/genie/benchmarks)).

Each new capability gets a row in `docs/verification-status.md` at the level it has
reached: `FAKES` until a real stack run, `REAL-E2E` only after one.

### 8.5 Reading the results honestly

Report effect sizes with the paired test, not a bare delta. A promotion no canary question
covers is reported as uncovered. A result from a scripted reviewer is labelled a
mechanism test. Benchmarks contain wrong reference answers (Snowflake notes this for BIRD,
[V](https://www.snowflake.com/en/engineering-blog/agentic-semantic-model-text-to-sql)),
so disagreements between the golden set and a reviewer are adjudicated by a human.

### 8.6 The immediate experiment: KB as Terms vs as evidence

Question: how much of the 27-30% with KB is lost or gained when the knowledge is retrieved
from the ontology instead of handed to the model verbatim?

Arms, on the same 180 tasks, same model, same number of runs, scored with `lsb-score` and
its paired test:

| Arm | Knowledge |
|---|---|
| A | none (the ~0% baseline, re-run on the same commit) |
| B | KB passed as evidence (`--oracle`, the 27-30% reference, re-run) |
| C | KB entries loaded as `term` and `sql_attribute` items through the document-import path, certified in a scratch database by a scripted import (never production), retrieved by Lexis normally; evidence empty |
| D | C plus dependency-chain retrieval (hierarchical KB items pulled together) |

Predictions, written before the run so they can fail [I]: C is at or below B, because
retrieval can miss; D recovers part of the gap on multi-hop tasks. Reading: B minus C is
the **retrieval and representation loss** of the ontology; C minus A is the value of the
ontology as built; D minus C is the value of chain retrieval. Report per-task retrieval
recall (was the needed KB item in the prompt?) separately from accuracy (was it used?),
the split BIRD-History makes between retrieving and using
([E, preprint](https://arxiv.org/abs/2608.29345)).

Rules: split the tasks by database into dev and held-out **before** seeing any result and
tune only on dev. The loader decides the KB-to-item mapping generically; the
benchmark's `sol_sql` and `test_cases` are not public
([E](https://github.com/bird-bench/livesqlbench)), and the loader code must contain no
task-specific strings (the integrity scan checks shipped code). The 180-task subset,
benchmark version and metric may differ from the official leaderboard, so this is a
within-Lexis comparison, not a leaderboard claim.

## 9. Phased delivery plan

Each phase is independently shippable behind a setting and has a measured acceptance
criterion. Effort is the author's estimate for one engineer who knows the repo [I]; read
it as order of magnitude.

| Phase | Scope | Effort | Expected impact [I] | Acceptance |
|---|---|---|---|---|
| **P0: Foundations and measurement** | Tables and migration (3.4); transition function and permissions (7.2); events and the replay test; golden-set slices and stream mode in `dev_tools/evals`; runtime contamination hash; the KB-as-Terms experiment (8.6); `answer_provenance` and a read-only "knowledge used" panel | 3-4 weeks | No accuracy change by itself. The first honest number for what the ontology loses against evidence, and the baseline for every later phase | Replay reproduces the projection; experiment report with paired test added to `docs/evaluation.md`; verification rows added |
| **P1: Capture and personal learning** | Clarification answers to personal drafts; personal-draft retrieval with the scope filter; edit-and-run capture; thumbs; "saved as your draft" UX; episode table | 3 weeks | Fewer repeated clarifications for the same user; accuracy gain on repeated phrases | Repeat-clarification rate down on the scripted stream; no change on held-out for other users (isolation test passes) |
| **P2: Review, promotion, verified reuse** | Queue, diff, certify and reject, audit log; materialisation into `term` and `sql_attribute`; canary gate; `verified_query` and the Verified badge; the query-history suggester re-enabled as draft-only | 4-5 weeks | Certified coverage grows; shared accuracy gain; Verified answers with measured accuracy | Held-out accuracy up over the stream with a scripted reviewer; no canary regression; Verified-answer accuracy at or above a threshold fixed in advance |
| **P3: Consolidation and routing** | Consolidation job (dedup, conflicts, revalidation, decay, weekly queue); fast/slow routing with multi-candidate agreement and the term coverage check; calibrated confidence and the ask rule; anomaly detection and quotas; shadow mode | 5-6 weeks | Fewer stale or conflicting items; fewer wrong-confident answers; cost savings on the fast path | Forgetting and drift tests pass; ECE reported; clarification utility up with accuracy not down |

Order rationale: P0 comes before anything writes (README roadmap item 5 before 7). P1 comes
before P2 because personal scope gives value without a reviewer. The suggester returns
only after P2, because its past failure was the absence of review. The term coverage check
and multi-candidate agreement are cheap enough to start inside P1 if the maintainer wants
an accuracy gain sooner (rated high impact in the research roadmap, [I]).

## 10. Risks and decisions for the maintainer

### 10.1 Risks

| Risk | Mitigation |
|---|---|
| Reviewer fatigue; the queue becomes a rubber stamp | Bounded weekly queue, bulk actions, approve-rate audit |
| Learned knowledge does not move held-out accuracy | P0's experiment bounds the ceiling first; P1 is cheap and reversible behind a flag |
| Two writers of the same facts (compilation and learning) | Learning writes only by materialising certified items; compilation keeps its source tags; the conflict job flags overlaps |
| Evaluation confounded by a scripted reviewer or user | Label results as mechanism tests; require a small real-human run before a product claim |
| Schema drift invalidates much of the KB at once | Fingerprint revalidation; `needs_revalidation` is still served but unbadged |
| Event log growth | Partition by month; retention on `episode`, never on `knowledge_event` |
| The `messages` table is owned by Prisma (the clarification module notes it has no structured-parts column) | Store episodes in the Python-owned schema, reference message ids, never alter `messages` |
| Over-claiming | Every number in a doc, PR or the README carries its tag and report label; the verification register is the arbiter |

### 10.2 Decisions required from the maintainer

1. Is certification human-only forever, including bulk document imports? (This design: yes.)
2. Who may certify in shared scope: the domain owner (`owner_id`), a new reviewer role, or admins only? Is a second reviewer required for high-impact Terms, and who defines "high-impact"?
3. Are personal drafts visible to admins for audit? They contain user phrasing: privacy against oversight.
4. Retention of `episode` (proposal 90 days), and whether question text may be stored at all for customers with strict data policies.
5. Is team scope worth building in P2, or do `personal` and `shared` suffice until a customer asks? Team adds permission surface.
6. The canary tolerance, and what happens to a promotion no canary question covers: block it, or allow it with the "uncovered" mark?
7. Show confidence numerically, or only route and badge, until calibration is demonstrated?
8. Who writes golden sets and expected rows for customers' own databases? The evaluation needs ground truth the product does not have.
9. Keep the `*_certified` booleans as the read model long term, or migrate readers to `status` and drop them in a later major step?
10. Is French a first-class `lang` in `term_label` from P0 (the roadmap lists French questions as later)?

### 10.3 Decisions taken (2026-11-12)

The maintainer accepted the author's recommendation on every question except 10.

| # | Decision |
|---|---|
| 1 | Certification is human only, forever. A bulk document import may offer "accept all" but it is still one human action, recorded per item. |
| 2 | In shared scope, the domain owner (`owner_id`) and admins certify. No new reviewer role and no second reviewer until a customer asks. |
| 3 | Personal drafts are not visible to admins by default. An opt-in audit setting makes them visible, for organisations that need it. |
| 4 | `episode` is kept 90 days. A setting turns off storing the question text at all. |
| 5 | No team scope for now: `personal` and `shared` only. |
| 6 | Canary tolerance is zero regressions. A promotion no canary question covers is allowed, with the "uncovered" mark. |
| 7 | No numeric confidence until calibration is shown. Answers show badges and the knowledge-used list. |
| 8 | Golden sets for a customer's databases are written by the customer with our help; answers marked Verified become candidate cases. Demos use Pagila, Chinook and LiveSQLBench. |
| 9 | Readers move to `status`; the `*_certified` booleans are kept during the transition and dropped in a later major step. |
| 10 | **Not as proposed.** The ontology stays English only: Terms, definitions, labels and SqlAttributes are written once, in English, and `term_label` has no `lang`. Questions in other languages are handled at answer time: (a) a question detected as non-English is translated to English for retrieval only (one short model call, skipped for English); (b) SQL generation sees both the original and the English question; (c) stored values are never translated (value linking handles them); (d) the prose answer and clarification questions are in the user's language. Reason: the embedding model is English-centred, so a French question retrieves English Terms worse, and keeping one language avoids two label sets drifting apart. It ships behind a setting and is enabled only after a measurement on the held-out Chinook questions translated to French (English, French without translation, French with translation). |

Two further choices of the same day: the LiveSQLBench knowledge entries loaded as Terms stay loaded for the document experiment (8.6 and variant E), and the experiment's wiki texts are kept outside this repository.

## 11. Key design decisions in this document

| # | Decision | Alternative rejected | Basis |
|---|---|---|---|
| 1 | Drafts live in new tables; certified items are materialised into the existing `term` and `sql_attribute` | Add status and scope columns to the existing tables | Global unique `sql_attribute.name` and unique `(name, source)` on terms make personal and conflicting drafts impossible there; materialisation keeps compile, index and glossary unchanged |
| 2 | Event-sourced append-only `knowledge_event` with PROV-like fields; current state is a projection | Mutable rows with a side audit table | Audit and rollback are the enterprise story; Palantir's actions-with-audit model ([E](https://palantir.com/docs/foundry/action-types/overview/)), applied to Lexis by inference [I] |
| 3 | The agent has no certify capability, enforced in code and transition rules; personal scope isolates drafts | Auto-promotion above a confidence threshold | MINJA-class poisoning ([E, preprint](https://arxiv.org/abs/2503.03704)); the suggester's past failure |
| 4 | Verified badge only for unchanged reuse of a certified SqlAttribute or verified query | Badge whenever certified knowledge was used | Databricks Trusted semantics ([E](https://docs.databricks.com/aws/en/genie/trusted-assets)); a smaller, defensible claim |
| 5 | Retrieval order certified shared > personal drafts > other drafts, labels carried in metadata and not only in prose | Treat all knowledge equally | Trust and auditability; ranking is not truth |
| 6 | Consolidation emits deltas and never rewrites; dedup and conflicts produce review tasks | Online rewriting by the agent | ACE's context-collapse finding ([E, authors' results](https://arxiv.org/html/2510.04618v2)) and the CLS rationale |
| 7 | Promotion gated by a canary replay; held-out slice invisible to the pipeline; runtime contamination hash | Evaluate after the fact | Genie's benchmark separation ([E](https://docs.databricks.com/aws/en/genie/benchmarks)); the repo's integrity rules |
| 8 | P0 is measurement plus the KB-as-Terms experiment, before any write path ships | Build capture first | Without a baseline no later claim is measurable; the experiment bounds the achievable gain |
