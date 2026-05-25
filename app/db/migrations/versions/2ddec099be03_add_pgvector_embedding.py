"""add_pgvector_embedding

Revision ID: [garde l'id généré]
Revises: 3417883208c9
Create Date: [garde la date]
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '[garde ton id]'
down_revision: Union[str, Sequence[str], None] = '3417883208c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Active pgvector
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Ajoute colonne vector sur document_chunks
    op.execute("""
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS embedding_vector vector(1024)
    """)

    # Index HNSW pour recherche cosine ultra-rapide
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding_vector
        ON document_chunks
        USING hnsw (embedding_vector vector_cosine_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding_vector")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_vector")