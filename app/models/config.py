import uuid
from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PlatformConfig(Base, TimestampMixin):
    __tablename__ = "platform_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="global", nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(100))