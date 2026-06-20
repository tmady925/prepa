"""add_monthly_job_quota

Revision ID: aaa010
Revises: aaa009
Create Date: 2026-06-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa010'
down_revision: Union[str, Sequence[str], None] = 'aaa009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reset mensuel du quota offres : quand reset_at <= now, on repart à 0.
    op.add_column(
        'candidate_profiles',
        sa.Column('job_offers_month_reset_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('candidate_profiles', 'job_offers_month_reset_at')
