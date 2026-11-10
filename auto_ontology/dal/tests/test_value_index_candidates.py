# SPDX-License-Identifier: Apache-2.0

"""What the value index reads from the catalog: keys and PII tags included."""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("sqlalchemy")

from auto_ontology.dal import datasources as d  # noqa: E402
from auto_ontology.dal import schema as s  # noqa: E402
from auto_ontology.dal.session import store  # noqa: E402
from auto_ontology.dal.tags import (  # noqa: E402
    PII_TAG_NAME,
    TARGET_COLUMN,
    attach_tag,
    get_or_create_tag,
    get_tag_by_name,
)


@pytest.fixture(scope="module", autouse=True)
def require_schema():
    if not os.environ.get("POSTGRES_USER"):
        pytest.skip("POSTGRES_* not set")
    try:
        store().query_read("SELECT 1 FROM column_attribute LIMIT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"auto_ontology schema unavailable (alembic upgrade head): {exc}")


def _add(table, **values) -> str:
    return store().query_write(table.insert().values(**values).returning(table.c.id))[
        0
    ]["id"]


@pytest.fixture
def catalog():
    name = f"t-{uuid.uuid4().hex[:8]}"
    other = f"{name}-other"
    created_pii = get_tag_by_name(PII_TAG_NAME) is None
    pii = get_or_create_tag(name=PII_TAG_NAME)

    def table(database_name: str, table_name: str, pk: list[str] | None) -> str:
        database = _add(s.catalog_database, name=database_name)
        schema = _add(s.catalog_schema, database_id=database, name="public")
        return _add(s.catalog_table, schema_id=schema, name=table_name, pk=pk)

    customer = table(name, "customer", ["customer_id"])
    columns = {
        column: _add(
            s.catalog_column,
            table_id=customer,
            name=column,
            data_type=data_type,
            ordinal_position=position,
        )
        for position, (column, data_type) in enumerate(
            [
                ("customer_id", "integer"),
                ("last_name", "text"),
                ("country", "text"),
            ],
            start=1,
        )
    }
    _add(
        s.catalog_column,
        table_id=table(other, "city", None),
        name="city",
        data_type="text",
        ordinal_position=1,
    )
    attach_tag(tag_id=pii["id"], kind=TARGET_COLUMN, item_id=columns["last_name"])

    yield name

    for database_name in (name, other):
        store().query_write(
            s.catalog_database.delete().where(
                s.catalog_database.c.name == database_name
            )
        )
    if created_pii:
        store().query_write(s.tag.delete().where(s.tag.c.id == pii["id"]))


def test_columns_come_back_with_their_key_and_pii_state(catalog: str) -> None:
    rows = {r["column_name"]: r for r in d.fetch_value_index_candidates(catalog)}

    assert set(rows) == {"customer_id", "last_name", "country"}
    assert rows["customer_id"]["is_primary_key"] is True
    assert rows["country"]["is_primary_key"] is False
    assert rows["last_name"]["pii_tagged"] is True
    assert rows["country"]["pii_tagged"] is False
    assert rows["country"]["pii_processed"] is False
    assert rows["country"]["data_type"] == "text"
    assert rows["country"]["table_name"] == "customer"
    assert rows["country"]["schema_name"] == "public"


def test_only_the_named_database_is_read(catalog: str) -> None:
    (row,) = d.fetch_value_index_candidates(f"{catalog}-other")

    assert row["column_name"] == "city"
    assert d.fetch_value_index_candidates("no-such-database") == []
