"""add_notification_queue

Revision ID: aaa007
Revises: aaa006
Create Date: 2026-06-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aaa007'
down_revision: Union[str, Sequence[str], None] = 'aaa006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column(
        'notification_queue',
        postgresql.JSONB(),
        nullable=False,
        server_default='[]',
    ))


def downgrade() -> None:
    op.drop_column('users', 'notification_queue')
