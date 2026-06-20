"""recruiter_monthly_quota

Revision ID: aaa011
Revises: aaa010
Create Date: 2026-06-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa011'
down_revision: Union[str, Sequence[str], None] = 'aaa010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('recruiters', sa.Column('annonces_month_reset_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('recruiters', 'annonces_month_reset_at')
