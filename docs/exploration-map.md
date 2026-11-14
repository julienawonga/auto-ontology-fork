<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
SPDX-License-Identifier: Apache-2.0
-->

# Exploration map: business domains

The exploration map has three levels. The first level shows one bubble per
database. Click a database and you see one bubble per **business domain** —
Finance, Marketing, Sales and so on — with the links between the domains.
Click a domain and you see the map of its Terms, as before.

## What each domain bubble shows

Each bubble is one business domain. Its size comes from the number of Terms in
it, and hovering or selecting it opens a side panel with the details:

- the domain's name and description,
- how many Terms it holds and how many tables those Terms touch,
- how many of its Terms are fully certified ("x of n certified"),
- how many column and SQL attributes its Terms carry,
- how many Term-to-Term links stay inside the domain,
- its top Terms (the three with the most relationships), and
- the strongest neighbouring domains, with the weight of each link.

Domains that a person has edited carry a small "edited" badge.

## What a link between two domains means

A line between two domains summarises the Term-to-Term links between them: the
line's width comes from the number of links, and its label gives the number of
links and the number of tables the two domains share (two Terms are linked when
they share a table). With many links on screen the labels are shown only on
hover or focus. Hovering a single line shows its weight, the shared tables and
the relationship types involved.

## How domains are created

A database's domains are produced by the model: it groups the database's Terms
into business domains and names them the way a manager would. After that a
person can rename a domain or move Terms between domains.

Regenerating the domains does not throw that work away: edited domains and
Terms a person moved are kept, and only the unedited, model-generated parts are
replaced. Terms created after the last generation show up under **Unassigned**
until the domains are regenerated.

## Endpoints

| Route | Does |
|---|---|
| `GET /api/exploration/semantic-domains?database_id=...` | Returns a database's stored domains, the cross-domain links and whether domains have been generated at all. |
| `POST /api/exploration/semantic-domains/generate?database_id=...` | Asks the model to group the database's Terms and stores the result. This can take 10 to 60 seconds. |
| `PATCH /api/exploration/semantic-domains/{domain_id}` | Renames a domain (and marks it as edited). |
| `POST /api/exploration/semantic-domains/{domain_id}/terms` | Moves Terms into a domain (and marks the assignments as edited). |

If a database has no domains yet, the map shows its Terms as before, with a
button to generate the domains.

## Limits

- The grouping quality on a real catalog is **not measured yet**: the domains
  come from one model call, and no evaluation has checked how sensible the
  groups are.
- A Term has **at most one domain per database**. A Term that appears in
  several databases can belong to one domain in each.
- Terms created after the last generation appear under **Unassigned** until
  someone regenerates the database's domains.
