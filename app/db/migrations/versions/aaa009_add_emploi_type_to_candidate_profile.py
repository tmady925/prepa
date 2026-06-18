"""add_emploi_type_to_candidate_profile

Revision ID: aaa009
Revises: aaa008
Create Date: 2026-06-18
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa009'
down_revision: Union[str, Sequence[str], None] = 'aaa008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'candidate_profiles',
        sa.Column(
            'emploi_type',
            sa.String(20),
            nullable=True,
            comment='petit_job | entreprise | les_deux — déduit par LLM à la fin de l\'onboarding emploi',
        ),
    )
    op.create_index('ix_candidate_profiles_emploi_type', 'candidate_profiles', ['emploi_type'])


def downgrade() -> None:
    op.drop_index('ix_candidate_profiles_emploi_type', 'candidate_profiles')
    op.drop_column('candidate_profiles', 'emploi_type')
