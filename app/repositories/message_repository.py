from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.message import Message
import uuid
import json
import httpx
from app.core.settings import get_settings

settings = get_settings()


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

    async def get_session_summary(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        user,
    ) -> str:
        """
        Retourne un résumé structuré de la session en cours.
        Remplace les 10 messages bruts par un contexte compact et utile.

        Format retourné :
        - Résumé de la session (3 phrases max)
        - Dernière question posée
        - Matière/chapitre en cours
        - Points de confusion détectés
        """
        conv_state = user.conversation_state or {}

        # Récupère les 6 derniers messages pour le résumé
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.created_at.desc())
            .limit(6)
        )
        messages = result.scalars().all()
        messages = list(reversed(messages))

        if not messages:
            return ""

        # Si seulement 1-2 messages → pas besoin de résumé
        if len(messages) <= 2:
            history = []
            for msg in messages:
                role = "user" if msg.direction == "inbound" else "assistant"
                history.append({"role": role, "content": msg.content[:200]})
            return ""

        # Construit le résumé
        parts = []

        # Contexte de la session
        if conv_state.get("matiere"):
            parts.append(f"Matière en cours : {conv_state['matiere']}")
        if conv_state.get("chapitre"):
            parts.append(f"Chapitre : {conv_state['chapitre'].replace('_', ' ')}")

        # Résumé LLM de la conversation
        summary = await self._summarize_conversation(messages)
        if summary:
            parts.append(summary)

        return "\n".join(parts) if parts else ""

    async def get_structured_context(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        user,
        mastery_context: str = "",
    ) -> tuple[list[dict], str]:
        """
        Retourne (history courte, contexte_memoire).

        history courte = 4 derniers échanges (au lieu de 10)
        contexte_memoire = résumé session + mémoire pédagogique

        Avantages vs 10 messages bruts :
        - 60% moins de tokens
        - Contexte plus pertinent
        - Mémoire pédagogique intégrée
        """
        # 1. Historique court — 4 derniers échanges seulement
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.created_at.desc())
            .limit(4)
        )
        recent = list(reversed(result.scalars().all()))

        history = []
        for msg in recent:
            role = "user" if msg.direction == "inbound" else "assistant"
            # Tronque les messages trop longs
            content = msg.content[:400] if len(msg.content) > 400 else msg.content
            history.append({"role": role, "content": content})

        # 2. Contexte mémoire structuré
        memory_parts = []

        # Résumé de session si plus de 4 messages
        total_result = await db.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        all_recent = list(reversed(total_result.scalars().all()))

        if len(all_recent) > 4:
            older = all_recent[:-4]  # Messages avant les 4 derniers
            summary = await self._summarize_conversation(older)
            if summary:
                memory_parts.append(f"Résumé session : {summary}")

        # Mémoire pédagogique
        if mastery_context:
            memory_parts.append(f"Profil élève : {mastery_context}")

        # État conversation
        conv_state = user.conversation_state or {}
        if conv_state.get("awaiting_answer"):
            memory_parts.append("L'élève est en train de répondre à un exercice.")

        memory_context = "\n".join(memory_parts) if memory_parts else ""

        return history, memory_context

    async def _summarize_conversation(self, messages: list) -> str:
        """
        Résume une liste de messages en 2-3 phrases via LLM.
        Extrait : sujets abordés, points de confusion, progression.
        """
        if not messages:
            return ""

        if not settings.mistral_api_key:
            # Fallback texte : première + dernière question de l'élève
            inbound = [m for m in messages if m.direction == "inbound"]
            if not inbound:
                return ""
            parts = []
            if inbound[0].content:
                parts.append(f"Début de session : {inbound[0].content[:100]}")
            if len(inbound) > 1 and inbound[-1].content:
                parts.append(f"Dernière question : {inbound[-1].content[:100]}")
            return " | ".join(parts)

        # Formate les messages pour le prompt
        conv_text = ""
        for msg in messages[-8:]:
            role = "Élève" if msg.direction == "inbound" else "Prepa"
            conv_text += f"{role}: {msg.content[:200]}\n"

        if not conv_text.strip():
            return ""

        prompt = f"""Résume cette conversation pédagogique en 2-3 phrases courtes.
Indique : sujet principal, ce que l'élève a compris, ce qu'il n'a pas compris.

Conversation :
{conv_text}

Réponds avec un résumé factuel en français, 2-3 phrases maximum."""

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.1,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    return ""
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"Summarize error: {e}")
            return ""


message_repo = MessageRepository()