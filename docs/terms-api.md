<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Business Terms API

Direct, auditable create, update and delete of business Terms. Before these
routes the only way to add a Term was a whole-model import
(`POST /api/model/import`) and the only way to remove one was export plus
`import?replace=true`, which also deletes every other semantic not in the payload.

| Route | Permission (web app) | Does |
|---|---|---|
| `POST /api/terms` | `catalog:edit` on the account role | Creates a Term, links it to tables, embeds it. 201. |
| `PATCH /api/terms/{id}` | read at the proxy; the backend requires `catalog:edit` for the account role or the Term's owner group | Edits name, description, certification, owner and, new, `table_ids`. |
| `DELETE /api/terms/{id}` | same as PATCH | Deletes the Term, its table links, its ColumnAttributes and its index rows. |

The backend trusts the `x-auto-ontology-user-id` header the web app adds (see
[authentication](./authentication.md)); with no header the permission check is
skipped, as for every other backend write, so the backend must stay private.

## Create

```json
POST /api/terms
{
  "name": "Net revenue",
  "description": "Order total minus refunds, from payment.amount",
  "synonyms": ["net sales"],
  "database": "pagila",
  "table_ids": ["<table id>"],
  "owner_id": null
}
```

* Give `table_ids` (all in one database) or a `database` name or id. A Term
  belongs to a database only through the tables that represent it.
* Without `table_ids` the Term is linked to the tables whose name or columns its
  name and description mention (whole words, at least four characters, case
  insensitive). If nothing is mentioned the first table by name is used so the
  Term is still scoped to the database. The response's `linked_by` says
  `explicit`, `matched` or `fallback`; check it, a fallback link is a guess.
* Stored uncertified, source `semantic`, `created_by` = the caller. The source
  cannot be chosen.
* The Term's row is written to the semantic index immediately (delete by id,
  embed, write: one Term, no reindex). If the embedding endpoint fails the Term
  still exists and the response has `"embedded": false`; run
  `POST /api/semantic-compilation/reindex` for that database.
* Errors: 409 name already used (names are unique across Terms, including the
  ones compilation produced); 404 unknown database; 422 unknown table ids,
  tables of several databases, blank name.

## Update

`PATCH /api/terms/{id}` also accepts `table_ids` (replaces the links; at least
one, all in one database). A change of name, description or tables re-embeds the
Term's index row; a certification or owner change does not. A name already used
answers 409.

## Delete

`DELETE /api/terms/{id}` removes, in this order: the Term's vector rows and its
ColumnAttributes' vector rows (in every database, since a Term row is looked up
by id), then the Term, its table links, tag assignments and ColumnAttributes.
Index first: if the index delete fails nothing changed and the call can be
retried, whereas the other order could leave embeddings of a Term that no longer
exists.

* 404 when the Term does not exist, including one already deleted: the call is
  safe to repeat but the status is not idempotent.
* 409 while SqlAttributes still belong to the Term. They are user-written
  definitions and are never deleted implicitly; delete or move them first.
* Tables and columns are never touched.

## Audit

Each create, update and delete logs one INFO line, `audit: term.<action> ...`,
with the Term id and name and the caller's user id (`anonymous` when the header
is absent). There is no audit table; a change log is a separate piece of work.

## Known limits

* Compilation can rewrite the description of a `semantic` Term whose name and
  single table match what it produces; a hand-created Term with the same name on
  one table is then updated, not duplicated.
* Resetting a database's semantic layer deletes the Terms represented only by
  that database's tables, including hand-created ones.
