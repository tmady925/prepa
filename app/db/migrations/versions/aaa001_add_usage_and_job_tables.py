"""add_usage_and_job_tables

Revision ID: aaa001
Revises: 2ddec099be03
Create Date: 2026-06-04
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aaa001'
down_revision: Union[str, Sequence[str], None] = '2ddec099be03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── User — nouveaux champs emploi & usage ──────────────────────────
    op.add_column('users', sa.Column('usage', sa.String(20), nullable=True))
    op.add_column('users', sa.Column('secteur_emploi', postgresql.JSONB(), nullable=True))
    op.add_column('users', sa.Column('type_contrat_souhaite', sa.String(20), nullable=True))
    op.add_column('users', sa.Column('localisation_emploi', sa.String(100), nullable=True))
    op.add_column('users', sa.Column('niveau_etudes', sa.String(20), nullable=True))

    # ── Concours — enrichissement ──────────────────────────────────────
    op.add_column('concours', sa.Column('type_concours', sa.String(50), nullable=True))
    op.add_column('concours', sa.Column('organisme', sa.String(100), nullable=True))
    op.add_column('concours', sa.Column('conditions_inscription', postgresql.JSONB(), nullable=True))
    op.add_column('concours', sa.Column('date_concours', sa.DateTime(timezone=True), nullable=True))
    op.add_column('concours', sa.Column('date_inscription_limite', sa.DateTime(timezone=True), nullable=True))
    op.add_column('concours', sa.Column('lien_officiel', sa.Text(), nullable=True))
    op.add_column('concours', sa.Column('programme_text', sa.Text(), nullable=True))
    op.add_column('concours', sa.Column(
        'statut_concours', sa.String(20), nullable=False, server_default='upcoming'
    ))

    # ── Table recruiters ───────────────────────────────────────────────
    op.create_table(
        'recruiters',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('nom', sa.String(100), nullable=False),
        sa.Column('entreprise', sa.String(100), nullable=False),
        sa.Column('email', sa.String(100), unique=True, nullable=False),
        sa.Column('phone', sa.String(20), nullable=True),
        sa.Column('password_hash', sa.String(255), nullable=True),
        sa.Column('plan', sa.String(20), nullable=False, server_default='gratuit'),
        sa.Column('annonces_restantes', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('abonnement_expire_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paydunya_token', sa.String(255), nullable=True),
        sa.Column('statut', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('logo_url', sa.Text(), nullable=True),
        sa.Column('api_key', sa.String(64), unique=True, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_recruiters_email', 'recruiters', ['email'])
    op.create_index('ix_recruiters_api_key', 'recruiters', ['api_key'])

    # ── Table job_opportunities ────────────────────────────────────────
    op.create_table(
        'job_opportunities',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('recruiter_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('recruiters.id', ondelete='SET NULL'), nullable=True),
        sa.Column('titre', sa.String(200), nullable=False),
        sa.Column('entreprise', sa.String(100), nullable=False),
        sa.Column('secteur', sa.String(100), nullable=True),
        sa.Column('localisation', sa.String(100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('competences_requises', postgresql.JSONB(), nullable=True),
        sa.Column('niveau_etudes', sa.String(50), nullable=True),
        sa.Column('type_contrat', sa.String(20), nullable=True),
        sa.Column('email_candidature', sa.String(100), nullable=True),
        sa.Column('logo_url', sa.Text(), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, server_default='admin'),
        sa.Column('source_url', sa.Text(), nullable=True, unique=True),
        sa.Column('statut', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('is_featured', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vues', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('embedding', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_job_opportunities_statut', 'job_opportunities', ['statut'])
    op.create_index('ix_job_opportunities_secteur', 'job_opportunities', ['secteur'])
    op.create_index('ix_job_opportunities_recruiter_id', 'job_opportunities', ['recruiter_id'])

    # ── Table candidate_profiles ───────────────────────────────────────
    op.create_table(
        'candidate_profiles',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  unique=True, nullable=False),
        sa.Column('competences', postgresql.JSONB(), nullable=True),
        sa.Column('secteurs_interets', postgresql.JSONB(), nullable=True),
        sa.Column('niveau_etudes', sa.String(50), nullable=True),
        sa.Column('annees_experience', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('localisation', sa.String(100), nullable=True),
        sa.Column('disponibilite', sa.String(50), nullable=True),
        sa.Column('cv_text', sa.Text(), nullable=True),
        sa.Column('cv_url', sa.Text(), nullable=True),
        sa.Column('embedding', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_candidate_profiles_user_id', 'candidate_profiles', ['user_id'])

    # ── Table job_matches ──────────────────────────────────────────────
    op.create_table(
        'job_matches',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('job_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('job_opportunities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('score_match', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('notifie_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('statut', sa.String(20), nullable=False, server_default='notifie'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_job_matches_user_id', 'job_matches', ['user_id'])
    op.create_index('ix_job_matches_job_id', 'job_matches', ['job_id'])


def downgrade() -> None:
    # Tables dans l'ordre inverse (FK d'abord)
    op.drop_table('job_matches')
    op.drop_table('candidate_profiles')
    op.drop_table('job_opportunities')
    op.drop_table('recruiters')

    # Colonnes concours
    for col in ['statut_concours', 'programme_text', 'lien_officiel',
                'date_inscription_limite', 'date_concours', 'conditions_inscription',
                'organisme', 'type_concours']:
        op.drop_column('concours', col)

    # Colonnes users
    for col in ['niveau_etudes', 'localisation_emploi', 'type_contrat_souhaite',
                'secteur_emploi', 'usage']:
        op.drop_column('users', col)
