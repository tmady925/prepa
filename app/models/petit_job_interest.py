import uuid
from datetime import datetime
from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PetitJobInterest(Base, TimestampMixin):
    """Intérêt d'un candidat pour un petit job.

    Cycle de vie : notified → interested → unlocked
    - notified  : candidat notifié lors de la publication
    - interested: candidat a répondu "je suis intéressé"
    - unlocked  : offreur a payé → a reçu le numéro du candidat
    """
    __tablename__ = "petit_job_interests"
    __table_args__ = (
        UniqueConstraint("petit_job_id", "candidat_user_id", name="uq_pji_job_candidat"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    petit_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("petit_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidat_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # notified | interested | unlocked
    statut: Mapped[str] = mapped_column(String(20), default="notified", nullable=False, index=True)

    # Quand le candidat a dit "intéressé"
    interested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Quand l'offreur a débloqué
    unlocked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Token PayDunya pour traçabilité
    paydunya_token: Mapped[str | None] = mapped_column(String(200), nullable=True)
