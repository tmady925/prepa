import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class Recruiter(Base, TimestampMixin):
    __tablename__ = "recruiters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nom: Mapped[str] = mapped_column(String(100), nullable=False)
    entreprise: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Plan & abonnement
    plan: Mapped[str] = mapped_column(String(20), default="gratuit")  # gratuit/starter/pro
    annonces_restantes: Mapped[int] = mapped_column(Integer, default=2)
    abonnement_expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paydunya_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Statut
    statut: Mapped[str] = mapped_column(String(20), default="pending")  # pending/active/suspended

    # Branding
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Auth API (pour intégration future)
    api_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
