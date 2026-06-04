"""add_job_scraping_fields

Revision ID: aaa003
Revises: aaa002
Create Date: 2026-06-04
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa003'
down_revision: Union[str, Sequence[str], None] = 'aaa002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('job_opportunities', sa.Column('content_hash', sa.String(64), nullable=True))
    op.add_column('job_opportunities', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('job_opportunities', sa.Column('annee_publication', sa.Integer(), nullable=True))
    op.create_index('ix_job_opportunities_content_hash', 'job_opportunities', ['content_hash'])
    op.create_index('ix_job_opportunities_source_url', 'job_opportunities', ['source_url'])


def downgrade() -> None:
    op.drop_index('ix_job_opportunities_source_url', 'job_opportunities')
    op.drop_index('ix_job_opportunities_content_hash', 'job_opportunities')
    op.drop_column('job_opportunities', 'annee_publication')
    op.drop_column('job_opportunities', 'last_seen_at')
    op.drop_column('job_opportunities', 'content_hash')
