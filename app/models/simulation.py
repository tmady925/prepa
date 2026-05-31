import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, Boolean, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    titre: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(20), default="examen")
    exam_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("exams.id", ondelete="SET NULL"), nullable=True)
    concours_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("concours.id", ondelete="SET NULL"), nullable=True)
    matiere: Mapped[str | None] = mapped_column(String(50))
    serie: Mapped[str | None] = mapped_column(String(20))
    sujet_pdf_path: Mapped[str | None] = mapped_column(Text)
    correction_pdf_path: Mapped[str | None] = mapped_column(Text)
    bareme_pdf_path: Mapped[str | None] = mapped_column(Text)
    date_debut: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duree_minutes: Mapped[int] = mapped_column(Integer, default=240)
    series_eligibles: Mapped[list] = mapped_column(JSONB, default=list)
    statut: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    notif_j1_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    notif_debut_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    resultats_envoyes: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SimulationParticipation(Base):
    __tablename__ = "simulation_participations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("simulations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    statut: Mapped[str] = mapped_column(String(20), default="inscrit")
    copie_path: Mapped[str | None] = mapped_column(Text)
    copie_url: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer)
    mention: Mapped[str | None] = mapped_column(String(50))
    classement: Mapped[int | None] = mapped_column(Integer)
    feedback: Mapped[dict | None] = mapped_column(JSONB)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
