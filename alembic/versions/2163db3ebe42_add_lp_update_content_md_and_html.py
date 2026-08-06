"""add_lp_update_content_md_and_html

Revision ID: 2163db3ebe42
Revises: 045d88a5f87a
Create Date: 2026-08-05 19:06:27.259483

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2163db3ebe42"
down_revision: str | None = "045d88a5f87a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lp_updates", sa.Column("content_md", sa.Text(), nullable=True))
    op.add_column("lp_updates", sa.Column("content_html", sa.Text(), nullable=True))
    op.add_column(
        "communication_archive",
        sa.Column("archive_metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("communication_archive", "archive_metadata")
    op.drop_column("lp_updates", "content_html")
    op.drop_column("lp_updates", "content_md")
