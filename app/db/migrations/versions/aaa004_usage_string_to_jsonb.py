"""usage_string_to_jsonb

Revision ID: aaa004
Revises: aaa003
Create Date: 2026-06-04

Convertit users.usage de String(20) vers JSONB pour stocker une liste d'usages.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aaa004'
down_revision: Union[str, Sequence[str], None] = 'aaa003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convertit la colonne avec cast — les valeurs existantes ("etudes", "emploi", etc.)
    # sont wrappées dans un tableau JSON: "etudes" → '["etudes"]'
    op.execute("""
        ALTER TABLE users
        ALTER COLUMN usage TYPE JSONB
        USING CASE
            WHEN usage IS NULL THEN NULL
            WHEN usage = '' THEN NULL
            ELSE to_jsonb(ARRAY[usage])
        END
    """)


def downgrade() -> None:
    # Reconvertit en String(20) — prend le premier élément du tableau
    op.execute("""
        ALTER TABLE users
        ALTER COLUMN usage TYPE VARCHAR(20)
        USING CASE
            WHEN usage IS NULL THEN NULL
            WHEN jsonb_array_length(usage) > 0 THEN usage->>0
            ELSE NULL
        END
    """)
