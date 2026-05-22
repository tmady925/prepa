import uuid
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
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

    # Contexte examen
    exam_type: Mapped[str | None] = mapped_column(String(50))
    series: Mapped[str | None] = mapped_column(String(20))
    exam_date: Mapped[str | None] = mapped_column(DateTime(timezone=True))

    # Quotas
    daily_messages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_messages_bonus: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quota_reset_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))

    # Engagement
    streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engagement_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Parrainage
    referral_code: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    referred_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # Upsell
    last_upsell_shown_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    upsell_refused_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Meta
    language: Mapped[str] = mapped_column(String(5), default="fr")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)