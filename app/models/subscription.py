import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, TimestampMixin


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    # Plan
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | active | expired | cancelled | failed

    # Période
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Paiement
    amount_fcfa: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(50))
    # wave | orange_money | free_money | card

    # PayDunya
    paydunya_token: Mapped[str | None] = mapped_column(String(200), index=True)
    paydunya_invoice_token: Mapped[str | None] = mapped_column(String(200))
    paydunya_receipt_url: Mapped[str | None] = mapped_column(Text)
    paydunya_raw: Mapped[dict | None] = mapped_column(JSONB)
    # réponse brute PayDunya stockée pour audit

    # Parrainage
    referral_code_used: Mapped[str | None] = mapped_column(String(20))


class Referral(Base, TimestampMixin):
    __tablename__ = "referrals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    referrer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    referred_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # pending | active | paid | fraudulent

    bonus_messages_granted: Mapped[int] = mapped_column(Integer, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))