from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.message import Message
import uuid


class MessageRepository:

    async def save(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        direction: str,
        content: str,
        intent: str = None,
        llm_provider: str = None,
        tokens_used: int = 0,
        from_cache: bool = False,
        whatsapp_id: str = None,
    ) -> Message:
        msg = Message(
            user_id=user_id,
            direction=direction,
            content=content,
            intent=intent,
            llm_provider=llm_provider,
            tokens_used=tokens_used,
            from_cache=from_cache,
            whatsapp_id=whatsapp_id,
        )
        db.add(msg)
        await db.flush()
        return msg

    async def get_history(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        limit: int = 10,
    ) -> list[dict]:
        """
        Retourne les derniers messages formatés pour le LLM.
        Format : [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        """
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        messages = result.scalars().all()
        messages = list(reversed(messages))

        history = []
        for msg in messages:
            role = "user" if msg.direction == "inbound" else "assistant"
            history.append({"role": role, "content": msg.content})

        return history


message_repo = MessageRepository()