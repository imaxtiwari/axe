"""add pm_user onboarding_state

Revision ID: be01ebb59012
Revises: 13213e1afcdc
Create Date: 2026-07-30 12:14:18.219951

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be01ebb59012"
down_revision: str | None = "13213e1afcdc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pm_users",
        sa.Column(
            "onboarding_state",
            sa.String(length=32),
            nullable=False,
            server_default="not_started",
        ),
    )


def downgrade() -> None:
    op.drop_column("pm_users", "onboarding_state")
