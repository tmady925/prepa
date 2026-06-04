import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, Text, Float, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class JobOpportunity(Base, TimestampMixin):
    __tablename__ = "job_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Rattachement recruteur (optionnel — peut venir du scraping)
    recruiter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recruiters.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Contenu annonce
    titre: Mapped[str] = mapped_column(String(200), nullable=False)
    entreprise: Mapped[str] = mapped_column(String(100), nullable=False)
    secteur: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    localisation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    competences_requises: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    niveau_etudes: Mapped[str | None] = mapped_column(String(50), nullable=True)
    type_contrat: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)  # CDI/CDD/stage/freelance
    email_candidature: Mapped[str | None] = mapped_column(String(100), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance
    source: Mapped[str] = mapped_column(String(20), default="admin", index=True)  # admin/recruteur/scraping
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)  # unique pour dédup scraping

    # Statut & visibilité
    statut: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/active/expired
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vues: Mapped[int] = mapped_column(Integer, default=0)

    # Scraping — déduplication et suivi
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    annee_publication: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Embedding pour matching sémantique (pgvector JSONB fallback)
    embedding: Mapped[list | None] = mapped_column(JSONB, nullable=True)
