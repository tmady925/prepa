"""petit_job_interests + offreur_plan_expires_at

Revision ID: aaa013
Revises: aaa012
Create Date: 2026-06-20
"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from alembic import op

revision: str = 'aaa013'
down_revision: Union[str, Sequence[str], None] = 'aaa012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'petit_job_interests',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('petit_job_id', UUID(as_uuid=True),
                  sa.ForeignKey('petit_jobs.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('candidat_user_id', UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('statut', sa.String(20), nullable=False, default='notified'),
        sa.Column('interested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('unlocked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paydunya_token', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('petit_job_id', 'candidat_user_id', name='uq_pji_job_candidat'),
    )
    op.create_index('ix_pji_statut', 'petit_job_interests', ['statut'])
    # Plan mensuel offreur sur le user
    op.add_column('users', sa.Column('offreur_plan_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'offreur_plan_expires_at')
    op.drop_table('petit_job_interests')
