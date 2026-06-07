"""add_extra_job_offers_bonus

Revision ID: aaa006
Revises: aaa005
Create Date: 2026-06-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'aaa006'
down_revision: Union[str, Sequence[str], None] = 'aaa005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column(
        'extra_job_offers_bonus', sa.Integer(), nullable=False, server_default='0'
    ))


def downgrade() -> None:
    op.drop_column('users', 'extra_job_offers_bonus')
