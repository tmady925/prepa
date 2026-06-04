import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone_number: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(100))

    # Plan et statut
    plan: Mapped[str] = mapped_column(String(20), default="free", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="onboarding", nullable=False)
    onboarding_step: Mapped[str] = mapped_column(String(30), default="start", nullable=False)
    conversation_state: Mapped[dict | None] = mapped_column(JSONB, default=None)

    # Contexte examen
    exam_type: Mapped[str | None] = mapped_column(String(50))
    series: Mapped[str | None] = mapped_column(String(20))
    exam_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pays: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Matières choisies pendant onboarding
    subjects: Mapped[list | None] = mapped_column(JSONB)

    # Quotas
    daily_messages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_messages_bonus: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quota_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Engagement
    streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    streak_last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engagement_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Parrainage
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Upsell
    last_upsell_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upsell_refused_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Meta
    language: Mapped[str] = mapped_column(String(5), default="fr")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Usage plateforme
    usage: Mapped[str | None] = mapped_column(String(20), nullable=True)  # etudes/concours/emploi/tout

    # Profil emploi
    secteur_emploi: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    type_contrat_souhaite: Mapped[str | None] = mapped_column(String(20), nullable=True)
    localisation_emploi: Mapped[str | None] = mapped_column(String(100), nullable=True)
    niveau_etudes: Mapped[str | None] = mapped_column(String(20), nullable=True)