# SPDX-License-Identifier: Apache-2.0
"""Persistence for generated and human-edited business domains.

Human-edit rules, in one place:

* regeneration never overwrites human work — a domain with origin ``'human'``
  keeps its row and its human-assigned Terms, and only ``'auto'`` domains and
  assignments are replaced;
* a regenerated auto domain whose name equals an existing human domain's name
  merges into it instead of duplicating it;
* renaming a domain or moving Terms into one marks the domain (and the moved
  assignments) ``'human'``, so the next generation spares them.

Every write runs in one transaction (``write_transaction``), so a partial
regeneration can never leave a database with half its domains deleted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store, write_transaction


def fetch_domains(database_id: str) -> list[dict[str, Any]]:
    """Return the stored domains of a database with their assigned Term ids."""
    rows = store().query_read(
        select(s.domain)
        .where(s.domain.c.database_id == database_id)
        .order_by(s.domain.c.name, s.domain.c.id)
    )
    result = [dict(row) | {"term_ids": []} for row in rows]
    by_id = {row["id"]: row for row in result}
    assignments = store().query_read(
        select(s.term_domain.c.domain_id, s.term_domain.c.term_id)
        .where(s.term_domain.c.database_id == database_id)
        .order_by(s.term_domain.c.term_id)
    )
    for assignment in assignments:
        target = by_id.get(assignment["domain_id"])
        if target is not None:
            target["term_ids"].append(assignment["term_id"])
    return result


def replace_auto_domains(database_id: str, groups: list[dict[str, Any]]) -> None:
    """Replace generated rows while preserving human domains and assignments.

    Generated groups with a human domain's name contribute their Terms to that
    domain. Existing human assignments always win over generated assignments:
    only ``origin='auto'`` rows are deleted, and the per-database uniqueness on
    ``(term_id, database_id)`` keeps a human assignment in place when a generated
    group names the same Term.
    """
    with write_transaction():
        human_rows = store().query_read(
            select(s.domain.c.id, s.domain.c.name).where(
                s.domain.c.database_id == database_id,
                s.domain.c.origin == "human",
            )
        )
        human_by_name = {
            row["name"].strip().casefold(): row["id"] for row in human_rows
        }
        store().query_write(
            delete(s.term_domain).where(
                s.term_domain.c.database_id == database_id,
                s.term_domain.c.origin == "auto",
            )
        )
        store().query_write(
            delete(s.domain).where(
                s.domain.c.database_id == database_id, s.domain.c.origin == "auto"
            )
        )
        for group in groups:
            name = str(group["name"]).strip()
            if not name:
                continue
            domain_id = human_by_name.get(name.casefold())
            if domain_id is None:
                created = store().query_write(
                    insert(s.domain)
                    .values(
                        database_id=database_id,
                        name=name,
                        description=group.get("description"),
                        origin="auto",
                    )
                    .returning(s.domain.c.id)
                )
                domain_id = created[0]["id"]
            for term_id in dict.fromkeys(group.get("term_ids", [])):
                assignment = pg_insert(s.term_domain).values(
                    term_id=term_id,
                    domain_id=domain_id,
                    database_id=database_id,
                    origin="auto",
                )
                # A human assignment for the same (term_id, database_id) wins:
                # the unique constraint absorbs this insert instead of it.
                store().query_write(
                    assignment.on_conflict_do_nothing(
                        constraint="uq_term_domain_term_database"
                    )
                )


def rename_domain(domain_id: str, name: str, description: str | None = None) -> bool:
    """Rename a domain and mark it human-managed; return whether it existed.

    ``description`` is written only when given, so a rename carrying just a name
    keeps the stored description.
    """
    with write_transaction():
        existing = store().query_read(
            select(s.domain.c.id).where(s.domain.c.id == domain_id)
        )
        if not existing:
            return False
        values: dict[str, Any] = {"name": name.strip(), "origin": "human"}
        if description is not None:
            values["description"] = description
        store().query_write(
            update(s.domain).where(s.domain.c.id == domain_id).values(**values)
        )
        return True


def move_terms(domain_id: str, term_ids: list[str]) -> bool:
    """Move Terms into a domain and mark the result human-edited.

    Returns False when the domain does not exist; raises ValueError when any
    Term id is unknown, rolling the whole move back.
    """
    with write_transaction():
        domain_rows = store().query_read(
            select(s.domain.c.database_id).where(s.domain.c.id == domain_id)
        )
        if not domain_rows:
            return False
        database_id = domain_rows[0]["database_id"]
        if not term_ids:
            # Moving nothing is still a human decision about the domain.
            store().query_write(
                update(s.domain)
                .where(s.domain.c.id == domain_id)
                .values(origin="human")
            )
            return True
        valid = store().query_read(
            select(s.term.c.id).where(s.term.c.id.in_(set(term_ids)))
        )
        valid_ids = {row["id"] for row in valid}
        if valid_ids != set(term_ids):
            raise ValueError("Unknown Term ids")
        # One domain per Term per database: drop this database's assignment for
        # each moved Term (whatever domain it named) before inserting the new one.
        store().query_write(
            delete(s.term_domain).where(
                s.term_domain.c.term_id.in_(valid_ids),
                s.term_domain.c.database_id == database_id,
            )
        )
        for term_id in sorted(valid_ids):
            store().query_write(
                insert(s.term_domain).values(
                    term_id=term_id,
                    domain_id=domain_id,
                    database_id=database_id,
                    origin="human",
                )
            )
        store().query_write(
            update(s.domain).where(s.domain.c.id == domain_id).values(origin="human")
        )
        return True
