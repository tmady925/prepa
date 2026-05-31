import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, Float, ForeignKey, DateTime, Boolean, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin

# Import pgvector si disponible
try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Identification
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Namespace granulaire
    pays: Mapped[str | None] = mapped_column(String(50), index=True)
    exam_type: Mapped[str | None] = mapped_column(String(50), index=True)
    serie: Mapped[str | None] = mapped_column(String(20), index=True)
    matiere: Mapped[str | None] = mapped_column(String(50), index=True)
    chapitre: Mapped[str | None] = mapped_column(String(100), index=True)
    sous_chapitre: Mapped[str | None] = mapped_column(String(100))
    doc_type: Mapped[str | None] = mapped_column(String(30), index=True)

    # Métadonnées
    annee: Mapped[int | None] = mapped_column(Integer)
    niveau: Mapped[int] = mapped_column(Integer, default=2)
    source: Mapped[str | None] = mapped_column(String(100))
    langue: Mapped[str] = mapped_column(String(10), default="fr")

    # Stats
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    has_ocr: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stockage
    storage_url: Mapped[str | None] = mapped_column(Text)

    # Statut
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(100))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    # Contenu
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)

    # Namespace granulaire
    pays: Mapped[str | None] = mapped_column(String(50), index=True)
    exam_type: Mapped[str | None] = mapped_column(String(50), index=True)
    serie: Mapped[str | None] = mapped_column(String(20), index=True)
    matiere: Mapped[str | None] = mapped_column(String(50), index=True)
    chapitre: Mapped[str | None] = mapped_column(String(100), index=True)
    doc_type: Mapped[str | None] = mapped_column(String(30), index=True)
    annee: Mapped[int | None] = mapped_column(Integer)
    niveau: Mapped[int] = mapped_column(Integer, default=2)

    # Mots-clés pour hybrid search
    keywords: Mapped[list | None] = mapped_column(JSONB)

    # Embedding JSONB (fallback)
    embedding: Mapped[list | None] = mapped_column(JSONB)

    # Embedding pgvector (natif — utilisé si disponible)
    if PGVECTOR_AVAILABLE:
        embedding_vector: Mapped[list | None] = mapped_column(Vector(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )