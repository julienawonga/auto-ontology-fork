# SPDX-License-Identifier: Apache-2.0

"""add business domains and Term assignments

Revision ID: d04a88c1b790
Revises: a7c2e91d4b68
Create Date: 2026-11-13 15:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d04a88c1b790"
down_revision: Union[str, Sequence[str], None] = "a7c2e91d4b68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    ``domain`` holds one business domain per name inside a database; ``term_domain``
    assigns Terms to domains. ``database_id`` is denormalised onto ``term_domain`` on
    purpose: the UNIQUE(term_id, database_id) it enables means a Term has AT MOST
    ONE domain per database, while a cross-database Term can still carry one
    assignment per database it appears in.
    """
    op.create_table(
        "domain",
        sa.Column(
            "id",
            sa.Text(),
            server_default=sa.text("(gen_random_uuid())::text"),
            nullable=False,
        ),
        sa.Column("database_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), server_default="auto", nullable=False),
        sa.Column(
            "created",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["database_id"],
            ["catalog_database.id"],
            name=op.f("fk_domain_database_id_catalog_database"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain")),
        sa.CheckConstraint(
            "origin IN ('auto', 'human')", name=op.f("ck_domain_domain_origin")
        ),
    )
    op.create_index("ix_domain_database_id", "domain", ["database_id"], unique=False)
    op.create_table(
        "term_domain",
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("database_id", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), server_default="auto", nullable=False),
        sa.ForeignKeyConstraint(
            ["term_id"],
            ["term.id"],
            name=op.f("fk_term_domain_term_id_term"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domain.id"],
            name=op.f("fk_term_domain_domain_id_domain"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["database_id"],
            ["catalog_database.id"],
            name=op.f("fk_term_domain_database_id_catalog_database"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("term_id", "domain_id", name=op.f("pk_term_domain")),
        sa.CheckConstraint(
            "origin IN ('auto', 'human')",
            name=op.f("ck_term_domain_term_domain_origin"),
        ),
        sa.UniqueConstraint(
            "term_id", "database_id", name="uq_term_domain_term_database"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("term_domain")
    op.drop_index("ix_domain_database_id", table_name="domain")
    op.drop_table("domain")
