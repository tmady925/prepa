import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, Text, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pays: Mapped[str] = mapped_column(String(50), default="senegal")
    niveau: Mapped[str] = mapped_column(String(50), default="bac")
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Series(Base):
    __tablename__ = "series"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    exam_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exams.id", ondelete="CASCADE"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text)
    matieres: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Concours(Base):
    __tablename__ = "concours"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pays: Mapped[str] = mapped_column(String(50), default="senegal")
    niveau_requis: Mapped[str] = mapped_column(String(50), default="bac")
    description: Mapped[str | None] = mapped_column(Text)
    matieres_epreuves: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Enrichissement concours
    type_concours: Mapped[str | None] = mapped_column(String(50), nullable=True)  # grandes_ecoles/fonction_publique/prive/autre
    organisme: Mapped[str | None] = mapped_column(String(100), nullable=True)
    conditions_inscription: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    date_concours: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_inscription_limite: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lien_officiel: Mapped[str | None] = mapped_column(Text, nullable=True)
    programme_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    statut_concours: Mapped[str] = mapped_column(String(20), default="upcoming")  # upcoming/ongoing/passed


class ConcoursSeriesLink(Base):
    __tablename__ = "concours_series"

    concours_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("concours.id", ondelete="CASCADE"), primary_key=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True
    )
