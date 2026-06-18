"""add_petit_jobs

Revision ID: aaa008
Revises: aaa007
Create Date: 2026-06-18
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aaa008'
down_revision: Union[str, Sequence[str], None] = 'aaa007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'petit_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('employeur_user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('titre', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('type_travail', sa.String(100), nullable=True),
        sa.Column('lieu', sa.String(200), nullable=True),
        sa.Column('date_debut_str', sa.String(100), nullable=True),
        sa.Column('duree', sa.String(100), nullable=True),
        sa.Column('remuneration', sa.String(200), nullable=True),
        sa.Column('nb_postes', sa.Integer(), server_default='1'),
        sa.Column('statut', sa.String(20), server_default='ouvert'),
        sa.Column('nb_candidats_notifies', sa.Integer(), server_default='0'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_petit_jobs_statut', 'petit_jobs', ['statut'])
    op.create_index('ix_petit_jobs_lieu', 'petit_jobs', ['lieu'])
    op.create_index('ix_petit_jobs_employeur', 'petit_jobs', ['employeur_user_id'])


def downgrade() -> None:
    op.drop_table('petit_jobs')
