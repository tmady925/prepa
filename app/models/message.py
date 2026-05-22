import uuid
from sqlalchemy import String, Text, Integer, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    # Contenu
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    # inbound | outbound
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(20), default="text")

    # Contexte IA
    intent: Mapped[str | None] = mapped_column(String(50))
    llm_provider: Mapped[str | None] = mapped_column(String(30))
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)

    # WhatsApp
    whatsapp_id: Mapped[str | None] = mapped_column(String(100), index=True)