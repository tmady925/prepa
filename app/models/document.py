import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, Float, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Identification
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # pdf | docx | image

    # Namespace RAG
    exam_type: Mapped[str | None] = mapped_column(String(50))
    # bac_senegal | bfem | concours
    series: Mapped[str | None] = mapped_column(String(20))
    # S1 | S2 | L1...
    subject: Mapped[str | None] = mapped_column(String(50))
    # maths | physique | svt...
    doc_type: Mapped[str | None] = mapped_column(String(30))
    # cours | annale | fiche | exercice

    # Stats
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    has_ocr: Mapped[bool] = mapped_column(default=False)

    # Stockage
    storage_url: Mapped[str | None] = mapped_column(Text)
    # URL Cloudinary du fichier original

    # Statut
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | processing | indexed | error
    error_message: Mapped[str | None] = mapped_column(Text)

    uploaded_by: Mapped[str | None] = mapped_column(String(100))


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    # Contenu
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)

    # Namespace pour la recherche
    exam_type: Mapped[str | None] = mapped_column(String(50), index=True)
    series: Mapped[str | None] = mapped_column(String(20), index=True)
    subject: Mapped[str | None] = mapped_column(String(50), index=True)

    # Embedding — stocké comme vecteur pgvector
    # On utilise JSONB pour l'instant, on migrera vers vector() après
    embedding: Mapped[list | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )