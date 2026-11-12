# SPDX-License-Identifier: Apache-2.0

"""add ontology_language to catalog_database

Revision ID: a7c2e91d4b68
Revises: 99d4d343dd0b
Create Date: 2026-11-12 15:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c2e91d4b68"
down_revision: Union[str, Sequence[str], None] = "99d4d343dd0b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The server default fills every existing row with ``en``, so databases that
    were built before the column existed read as English.
    """
    op.add_column(
        "catalog_database",
        sa.Column(
            "ontology_language",
            sa.Text(),
            server_default="en",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("catalog_database", "ontology_language")
