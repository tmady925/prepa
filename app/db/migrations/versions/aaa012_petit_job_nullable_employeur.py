"""petit_job_nullable_employeur

Revision ID: aaa012
Revises: aaa011
Create Date: 2026-06-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa012'
down_revision: Union[str, Sequence[str], None] = 'aaa011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rend employeur_user_id nullable pour les petits jobs créés par l'admin
    op.alter_column('petit_jobs', 'employeur_user_id', nullable=True)


def downgrade() -> None:
    op.alter_column('petit_jobs', 'employeur_user_id', nullable=False)
