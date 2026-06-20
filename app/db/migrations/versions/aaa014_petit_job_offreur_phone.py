"""petit_job_offreur_phone

Revision ID: aaa014
Revises: aaa013
Create Date: 2026-06-20
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'aaa014'
down_revision: Union[str, Sequence[str], None] = 'aaa013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('petit_jobs', sa.Column('offreur_phone', sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column('petit_jobs', 'offreur_phone')
