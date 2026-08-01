"""add mnpi review queue

Revision ID: add_mnpi_review_queue
Revises: 2827983cf810
Create Date: 2026-08-01 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_mnpi_review_queue"
down_revision: str | None = "2827983cf810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mnpi_review_queue",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pm_id", sa.String(length=36), nullable=False),
        sa.Column("signal_id", sa.String(length=36), nullable=True),
        sa.Column("ticker", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("mnpi_score", sa.Float(), nullable=False),
        sa.Column("materiality_score", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("alert_payloads", sa.JSON(), nullable=False),
        sa.Column("reviewer_id", sa.String(length=36), nullable=True),
        sa.Column("decision_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pm_id"], ["pm_users.id"]),
        sa.ForeignKeyConstraint(["signal_id"], ["signal_log.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mnpi_review_queue_pm_created",
        "mnpi_review_queue",
        ["pm_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mnpi_review_queue_pm_created", table_name="mnpi_review_queue")
    op.drop_table("mnpi_review_queue")
