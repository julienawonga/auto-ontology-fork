<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Matching question wording to stored values

The chat answers from SQL a model writes, and a model writes the value the user
typed. When the data spells it differently, the filter matches nothing and the
answer is a confident zero:

| Question | The data stores | Without value linking |
|---|---|---|
| "How many customers are from the USA?" | `United States` | 0 (truth: 36) |
| "How many customers live in Indai?" | `India` | 0 (truth: 60) |

Differences of case already worked. Value linking covers abbreviations, typos,
plurals and punctuation, for the columns small enough to list.

It is **opt-in, per database**, and does nothing until you build an index.

## What is indexed, and what is not

Building the index **copies data out of the source database**: each distinct
value of an eligible column is stored as plain text, next to its table and column
name, in the semantic vector index (the `vdb.semantic_layer` table in the
Lexis Postgres), and is sent to your embedding endpoint.

A column is indexed only if all of these hold:

- it is a text column (`char`, `varchar`, `text`, `string`, `citext` or an enum);
- it is not a primary key;
- it is not tagged `PII`. With automatic PII detection on (Settings > PII
  Settings), a column that detection has not classified yet is left out too;
- its **name** does not say it holds personal data or secrets: a column named like
  an e-mail, phone, password, token, address, street, postal code, first or last
  name, user name, login, birth date, card or bank number is skipped, whatever its
  tags (the name is matched with its separators removed, so `first_name`,
  `FirstName` and `firstname` are one thing);
- its **values** do not look like contact details or secrets: a column is skipped
  when at least 10% of its distinct values look like e-mail addresses or card
  numbers, or 30% like phone numbers or hashes and tokens, whatever it is called;
- it has at most `VALUE_INDEX_MAX_DISTINCT` distinct values (2,000 by default),
  which also keeps most high-cardinality columns out;
- it is not free text: the mean value length is at most `VALUE_INDEX_MAX_AVG_LENGTH`
  characters (40), and no single value over 100 characters is kept;
- the database stays within `VALUE_INDEX_MAX_VALUES` rows in all (20,000). A
  column that would overflow it is skipped whole, never cut.

NULL and empty values are never indexed. Numbers, dates and other types are never
read.

**The name and value checks are a safety net, not a guarantee.** They exist
because the first real run of this index, on a stock demo database with PII
detection off and nothing tagged, copied customer e-mails, phone numbers, names,
addresses and a staff password column (and sent them to the embedding endpoint)
under the size limits alone; those rows were deleted and the rules above were
added. A personal value in a column with an ordinary name and ordinary-looking
values, such as a surname stored in a column called `label`, is still not
detected. Turn PII detection on (or tag the columns) and ingest before you build
the index on real data, and review the log, which names every column skipped and
why.

To index a column the name rule skipped, list it in `VALUE_INDEX_INCLUDE_COLUMNS`
(comma separated `table.column`, case-insensitive), for example
`customer.last_name`. That lifts the name rule for that column only: a `PII` tag
and the check on the values still apply.

Values are read with a `SELECT DISTINCT ... LIMIT` that goes through the same
read-only path as agent SQL (see [query-safety.md](./query-safety.md)) and the
connector's dialect and quoting.

## Building the index

```
POST /api/semantic-compilation/value-index?database_name=pagila
```

Permission `semanticCompilation: manage`, the same as the reindex. It never runs
by itself: ingestion and compilation do not call it. It runs synchronously: one
read per candidate column from the source, then one embedding call per value, so
a large schema takes a while.

```json
{ "database_name": "pagila", "expected": 120, "written": 118,
  "columns_indexed": 6, "columns_skipped": 70 }
```

`written < expected` means the embedding endpoint did not return a vector for
every value; the others are stored. `columns_skipped` is normally most of a
schema.

It can be repeated. Every value has a fixed id and replaces its previous row.
Rows that are no longer wanted are deleted: a value that left the data, a column
since tagged `PII` or grown past a limit. So tighten the rules, run it again, and
what they now exclude is removed.

Errors: 404 for a database the catalog does not hold, 409 while a semantic
compilation is running, 503 when no embedding endpoint or no connector for that
database is configured.

A semantic **reset** (which deletes every row of the database in the semantic
index) also deletes the value index; build it again afterwards. The semantic
index health report on `/api/semantic-compilation/status` does not count value
rows: the index is optional, so there is no expected number to compare with.

## How a question uses it

When a question reaches SQL generation without value anchors from the caller,
its phrases are looked up and the matches are added to the prompt as "verified
database values", with the table, the column and the exact stored spelling. Anchors
a caller supplies are never replaced or added to.

Phrases are the entities the question extraction found, and every 1 to 3 word
window of the question. A phrase that starts or ends with a stopword, has no
letter, or is the name of a table or column in scope is not looked up.

Each phrase goes through these, in order, and the first that finds something wins:

1. **A learned alias.** An alias a user confirmed for a stored value. There is
   no way to confirm one from the chat yet: `learn_value_alias` is the hook a
   later clarification step will call. The alias is stored in the metadata of
   that value's own row in the index, so it is scoped to its database, table and
   column, and is kept when the index is rebuilt.
2. **Exact, after normalisation.** Case, accents and punctuation are ignored, and
   a plural is read as its singular: `comedies` is `Comedy`, `pg 13` is `PG-13`.
   The values are held in memory, so this costs no embedding call. A longer phrase
   is taken whole: `United States` is one match, not `States` as well.
3. **A built-in alias.** A table of generic reference data
   (`retrieval/text_to_sql/value_aliases.py`): every ISO 3166-1 country, with its
   alpha-2 and alpha-3 codes and common names, and the postal codes of the US
   states. `US`, `USA`, `U.S.` and `America` all stand for `United States`;
   `Deutschland` for `Germany`. The table only proposes spellings: an anchor is
   made only if that exact value is stored in an indexed column, so a value the
   database lacks is never invented. It works in both directions (a user typing
   `United States` where `USA` is stored). An alias that means several things
   (`CA` is Canada and California) gives every reading that is stored, never one
   chosen silently; if the database holds only one of them, that one is the only
   reading there. No embedding model is involved, so it does not depend on one.
4. **Fuzzy.** What those missed is embedded and searched among the
   indexed values of this database (never another one), then re-ranked by
   character similarity (`difflib`). A hit is kept when its embedding similarity
   or its character similarity clears a threshold. A typo (`Indai`, `Brasil`) is
   carried by the character similarity. An abbreviation outside the built-in
   table shares almost no characters with its value and rests entirely on the
   embedding model placing the two close.

At most `VALUE_LINK_MAX_ANCHORS` anchors (5) are returned per question, at most
two per phrase, and the second only for another column that is nearly as good. A
phrase that matches nothing well gets no anchor, so a value that is not stored
(a country absent from the table) is left alone and is never "corrected".

It never blocks a question. A database with no index costs one cheap read of the
index table every 5 minutes and nothing else; a missing retriever, an embedding
endpoint that fails, or any other error means no anchors.

| Variable | Default | Meaning |
|---|---|---|
| `VALUE_INDEX_MAX_DISTINCT` | 2000 | Distinct values above which a column is skipped |
| `VALUE_INDEX_MAX_VALUES` | 20000 | Rows the index may hold for one database |
| `VALUE_INDEX_MAX_AVG_LENGTH` | 40 | Mean length above which a column is free text |
| `VALUE_LINK_MIN_SIMILARITY` | 0.80 | Embedding cosine similarity that keeps a fuzzy hit |
| `VALUE_LINK_MIN_LEXICAL` | 0.80 | Character similarity (0 to 1) that keeps a fuzzy hit |
| `VALUE_LINK_MAX_ANCHORS` | 5 | Anchors per question; 0 turns linking off |
| `VALUE_LINK_MAX_FUZZY_PHRASES` | 6 | Phrases sent to the embedding endpoint per question |
| `VALUE_LINK_CACHE_TTL_S` | 300 | Seconds the in-memory values are reused |

## Known limits

- **Abbreviations outside the built-in table depend on the embedding model.**
  Countries and US states do not (see the built-in aliases); anything else, such
  as an industry acronym, relies on the model you configured, which was not
  measured here. The built-in table is not a thesaurus: it holds codes and
  common names, not every nickname, and it has no city or province data.
  A two-letter alias such as `US` is also an English word, so "tell us" can
  anchor to `United States` in a database that stores it. If anchors are missing, lower `VALUE_LINK_MIN_SIMILARITY` a
  little at a time and check that a value which does not exist (see the held-out
  cases below) still gets none. Too low a threshold makes the engine invent a
  match, which is worse than a zero.
- Only small text columns are covered. Names, cities with thousands of values
  and any column over the limits are out of reach by design.
- A bare number is never looked up, and a phrase under three characters is never sent for fuzzy matching; those are left to the
  model.
- The index is a snapshot. Build it again after the data changes.
- Fuzzy matching sends each phrase to the embedding endpoint (up to
  `VALUE_LINK_MAX_FUZZY_PHRASES`, in parallel).

## Measuring it

Two case files, to be reported separately:

- `dev_tools/evals/cases/pagila_values.json` is a **development set**: 14
  questions over Pagila that include the examples which motivated the feature. It
  was used to design it, so it is not evidence of generalisation and must not be
  quoted as proof.
- `dev_tools/evals/cases/chinook_values.json` is the **held-out set**: 14
  questions over Chinook, which stores values differently (`USA`, `Hip Hop/Rap`),
  with other countries, aliases and spellings, and three values that do not exist.
  It was frozen before any measurement. Do not edit it after seeing a result.

Both include values that do not exist, where the right answer is zero. See
[evaluation.md](./evaluation.md) for how to run a set and the rules for an honest
comparison. Run each with and without the index, with the same cases, and report
the development and held-out scores side by side, never merged.
