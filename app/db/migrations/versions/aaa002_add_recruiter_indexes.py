"""add_recruiter_indexes

Revision ID: aaa002
Revises: aaa001
Create Date: 2026-06-04
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'aaa002'
down_revision: Union[str, Sequence[str], None] = 'aaa001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Index type_contrat pour filtrage rapide dans matching
    op.create_index('ix_job_opportunities_type_contrat', 'job_opportunities', ['type_contrat'])
    # Index source pour distinguer admin/recruteur/scraping
    op.create_index('ix_job_opportunities_source', 'job_opportunities', ['source'])
    # Index is_featured pour tri dashboard
    op.create_index('ix_job_opportunities_is_featured', 'job_opportunities', ['is_featured'])
    # Index statut recruteur pour filtrages admin
    op.create_index('ix_recruiters_statut', 'recruiters', ['statut'])
    # Index plan recruteur pour stats
    op.create_index('ix_recruiters_plan', 'recruiters', ['plan'])
    # Index job_matches statut pour notifications
    op.create_index('ix_job_matches_statut', 'job_matches', ['statut'])


def downgrade() -> None:
    op.drop_index('ix_job_matches_statut', 'job_matches')
    op.drop_index('ix_recruiters_plan', 'recruiters')
    op.drop_index('ix_recruiters_statut', 'recruiters')
    op.drop_index('ix_job_opportunities_is_featured', 'job_opportunities')
    op.drop_index('ix_job_opportunities_source', 'job_opportunities')
    op.drop_index('ix_job_opportunities_type_contrat', 'job_opportunities')
