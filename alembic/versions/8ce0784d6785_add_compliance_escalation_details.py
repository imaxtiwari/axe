"""add_compliance_escalation_details

Revision ID: 8ce0784d6785
Revises: adf8ba209e26
Create Date: 2026-08-14 00:55:13.059643

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8ce0784d6785'
down_revision: Union[str, None] = 'adf8ba209e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('compliance_escalation', sa.Column('details', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('compliance_escalation', 'details')
