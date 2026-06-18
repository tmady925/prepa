import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class CandidateProfile(Base, TimestampMixin):
    __tablename__ = "candidate_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Lié à un utilisateur WhatsApp (1-1)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True
    )

    # Profil extrait du CV
    competences: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    secteurs_interets: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    niveau_etudes: Mapped[str | None] = mapped_column(String(50), nullable=True)
    annees_experience: Mapped[int] = mapped_column(Integer, default=0)
    localisation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    disponibilite: Mapped[str | None] = mapped_column(String(50), nullable=True)  # immediate/1_mois/3_mois

    # CV brut
    cv_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cv_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # chemin local /cvs/ ou URL Cloudinary

    # Embedding pour matching sémantique (JSONB fallback pgvector)
    embedding: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Matching enrichi
    metiers_cibles: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    metiers_proches: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    competences_normalisees: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resume_profil: Mapped[str | None] = mapped_column(Text, nullable=True)
    type_contrat_souhaite: Mapped[str | None] = mapped_column(String(20), nullable=True)
    secteur_prioritaire: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nb_notifs_semaine: Mapped[int] = mapped_column(Integer, default=0)
    last_notif_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Type d'emploi préféré — déduit par LLM à la fin de l'onboarding
    # "petit_job" | "entreprise" | "les_deux"
    emploi_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)


class JobMatch(Base):
    __tablename__ = "job_matches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_opportunities.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    score_match: Mapped[float] = mapped_column(Float, default=0.0)
    notifie_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    statut: Mapped[str] = mapped_column(String(20), default="notifie")  # notifie/vu/ignore

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
