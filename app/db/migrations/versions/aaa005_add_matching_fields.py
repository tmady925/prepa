"""add_matching_fields

Revision ID: aaa005
Revises: aaa004
Create Date: 2026-06-04
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aaa005'
down_revision: Union[str, Sequence[str], None] = 'aaa004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── job_opportunities ──────────────────────────────────────────────
    op.add_column('job_opportunities', sa.Column('taches', postgresql.JSONB(), nullable=True))
    op.add_column('job_opportunities', sa.Column('conditions_requises', postgresql.JSONB(), nullable=True))
    op.add_column('job_opportunities', sa.Column('avantages', postgresql.JSONB(), nullable=True))
    op.add_column('job_opportunities', sa.Column('date_limite', sa.DateTime(timezone=True), nullable=True))
    op.add_column('job_opportunities', sa.Column('description_complete', sa.Text(), nullable=True))
    op.add_column('job_opportunities', sa.Column('experience_min_annees', sa.Integer(), nullable=True))
    op.add_column('job_opportunities', sa.Column('nb_notifications_sent', sa.Integer(), server_default='0'))

    # ── candidate_profiles ─────────────────────────────────────────────
    op.add_column('candidate_profiles', sa.Column('metiers_cibles', postgresql.JSONB(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('metiers_proches', postgresql.JSONB(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('competences_normalisees', postgresql.JSONB(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('resume_profil', sa.Text(), nullable=True))
    op.add_column('candidate_profiles', sa.Column('type_contrat_souhaite', sa.String(20), nullable=True))
    op.add_column('candidate_profiles', sa.Column('secteur_prioritaire', sa.String(100), nullable=True))
    op.add_column('candidate_profiles', sa.Column('nb_notifs_semaine', sa.Integer(), server_default='0'))
    op.add_column('candidate_profiles', sa.Column('last_notif_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ['nb_notifs_semaine', 'last_notif_at', 'secteur_prioritaire',
                'type_contrat_souhaite', 'resume_profil', 'competences_normalisees',
                'metiers_proches', 'metiers_cibles']:
        op.drop_column('candidate_profiles', col)
    for col in ['nb_notifications_sent', 'experience_min_annees', 'description_complete',
                'date_limite', 'avantages', 'conditions_requises', 'taches']:
        op.drop_column('job_opportunities', col)
