# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Postgres integration tests for business-domain persistence.

The rules that get a test each fail for a different reason:

* **human edits survive regeneration** — a human domain keeps its row, its
  origin and its human assignment while generated groups merge into it;
* **one domain per Term per database** — the denormalised UNIQUE(term_id,
  database_id) is the contract a second assignment must trip over;
* **cascade on database delete** — deleting a database removes its domains and
  assignments without leaving rows behind.
"""

import os
import uuid
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import delete, select  # noqa: E402

from auto_ontology.dal import domains  # noqa: E402
from auto_ontology.dal import schema as s  # noqa: E402
from auto_ontology.dal.session import store  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def require_schema() -> None:
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("POSTGRES_* not set")
    try:
        store().query_read(select(s.domain.c.id).limit(1))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"business-domain schema unavailable (alembic upgrade head): {exc}")


def _insert(table: Any, **values: Any) -> str:
    return store().query_write(table.insert().values(**values).returning(table.c.id))[
        0
    ]["id"]


def _cleanup(database_id: str, term_ids: list[str]) -> None:
    store().query_write(
        delete(s.catalog_database).where(s.catalog_database.c.id == database_id)
    )
    store().query_write(delete(s.term).where(s.term.c.id.in_(term_ids)))


def test_human_domains_and_assignments_survive_regeneration() -> None:
    prefix = uuid.uuid4().hex
    database_id = _insert(s.catalog_database, name=f"domains-{prefix}")
    term_a = _insert(s.term, name=f"A-{prefix}", source="semantic")
    term_b = _insert(s.term, name=f"B-{prefix}", source="semantic")
    human_id = _insert(
        s.domain, database_id=database_id, name="Finance", origin="human"
    )
    auto_id = _insert(s.domain, database_id=database_id, name="Old", origin="auto")
    store().query_write(
        s.term_domain.insert().values(
            term_id=term_a, domain_id=human_id, database_id=database_id, origin="human"
        )
    )
    store().query_write(
        s.term_domain.insert().values(
            term_id=term_b, domain_id=auto_id, database_id=database_id, origin="auto"
        )
    )

    # The generated group carries the human domain's name, so it merges into
    # that row instead of duplicating it; the auto domain and its assignment go.
    domains.replace_auto_domains(
        database_id,
        [{"name": "Finance", "description": "Generated", "term_ids": [term_a, term_b]}],
    )
    found = domains.fetch_domains(database_id)
    assert len(found) == 1
    assert found[0]["id"] == human_id
    assert found[0]["origin"] == "human"
    assert set(found[0]["term_ids"]) == {term_a, term_b}
    # The human assignment stays human even though regeneration names it.
    assignments = store().query_read(
        select(s.term_domain).where(s.term_domain.c.term_id == term_a)
    )
    assert assignments[0]["origin"] == "human"
    _cleanup(database_id, [term_a, term_b])


def test_one_assignment_per_term_per_database_and_database_cascade() -> None:
    prefix = uuid.uuid4().hex
    database_id = _insert(s.catalog_database, name=f"domains-{prefix}")
    term_id = _insert(s.term, name=f"Term-{prefix}", source="semantic")
    first = _insert(s.domain, database_id=database_id, name="First")
    second = _insert(s.domain, database_id=database_id, name="Second")
    store().query_write(
        s.term_domain.insert().values(
            term_id=term_id, domain_id=first, database_id=database_id, origin="auto"
        )
    )
    with pytest.raises(Exception):  # The database uniqueness guard is the contract.
        store().query_write(
            s.term_domain.insert().values(
                term_id=term_id,
                domain_id=second,
                database_id=database_id,
                origin="auto",
            )
        )
    # Deleting the database cascades to its domains and assignments.
    store().query_write(
        delete(s.catalog_database).where(s.catalog_database.c.id == database_id)
    )
    assert not store().query_read(
        select(s.domain.c.id).where(s.domain.c.id.in_([first, second]))
    )
    assert not store().query_read(
        select(s.term_domain.c.term_id).where(s.term_domain.c.term_id == term_id)
    )
    store().query_write(delete(s.term).where(s.term.c.id == term_id))


def test_rename_marks_human_and_move_terms_replaces_assignment() -> None:
    prefix = uuid.uuid4().hex
    database_id = _insert(s.catalog_database, name=f"domains-{prefix}")
    term_a = _insert(s.term, name=f"A-{prefix}", source="semantic")
    term_b = _insert(s.term, name=f"B-{prefix}", source="semantic")
    old = _insert(s.domain, database_id=database_id, name="Old", origin="auto")
    target = _insert(s.domain, database_id=database_id, name="Target", origin="auto")
    store().query_write(
        s.term_domain.insert().values(
            term_id=term_a, domain_id=old, database_id=database_id, origin="auto"
        )
    )

    # A rename with no description keeps the stored one and flips origin.
    assert domains.rename_domain(target, "Renamed") is True
    row = store().query_read(select(s.domain).where(s.domain.c.id == target))[0]
    assert row["name"] == "Renamed"
    assert row["origin"] == "human"

    # Moving Terms lands each one in the target domain, human-marked, replacing
    # the assignment the Term had in this database before.
    assert domains.move_terms(target, [term_a, term_b]) is True
    assignments = store().query_read(
        select(s.term_domain).where(s.term_domain.c.database_id == database_id)
    )
    assert {a["term_id"] for a in assignments} == {term_a, term_b}
    assert all(a["domain_id"] == target and a["origin"] == "human" for a in assignments)

    # An unknown Term id raises (rolling the move back); an unknown domain is False.
    with pytest.raises(ValueError):
        domains.move_terms(target, ["00000000-0000-0000-0000-000000000000"])
    assert domains.move_terms("00000000-0000-0000-0000-000000000000", [term_a]) is False
    assert domains.rename_domain("00000000-0000-0000-0000-000000000000", "X") is False
    _cleanup(database_id, [term_a, term_b])
