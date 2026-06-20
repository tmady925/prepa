import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PetitJob(Base, TimestampMixin):
    __tablename__ = "petit_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employeur_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    titre: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type_travail: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    lieu: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    date_debut_str: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duree: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remuneration: Mapped[str | None] = mapped_column(String(200), nullable=True)
    nb_postes: Mapped[int] = mapped_column(Integer, default=1)

    statut: Mapped[str] = mapped_column(String(20), default="ouvert", index=True)  # ouvert | pourvu | expiré
    nb_candidats_notifies: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
