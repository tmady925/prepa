import hashlib
import hmac
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.settings import get_settings
from app.db.database import get_db
from app.services.user_service import user_service

settings = get_settings()
router = APIRouter()


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Vérifie que le message vient bien de 360dialog."""
    expected = hmac.new(
        settings.whatsapp_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.get("/webhook")
async def webhook_verify(request: Request):
    """Vérification du webhook par 360dialog."""
    return {"status": "ok"}


@router.post("/webhook")
async def webhook_receive(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Point d'entrée de tous les messages WhatsApp."""
    payload = await request.body()
    data = await request.json()

    # Extrait les messages
    messages = (
        data.get("messages") or
        data.get("entry", [{}])[0]
        .get("changes", [{}])[0]
        .get("value", {})
        .get("messages", [])
    )

    if not messages:
        return {"status": "no_messages"}

    for message in messages:
        await process_message(message, db)

    return {"status": "ok"}


async def process_message(message: dict, db: AsyncSession):
    """Traite un message entrant."""
    phone = message.get("from")
    msg_type = message.get("type", "text")

    if not phone:
        return

    # Récupère ou crée l'élève
    user, created = await user_service.get_or_create(db, phone)

    # Extrait le texte
    text = ""
    if msg_type == "text":
        text = message.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive["button_reply"].get("id", "")
        elif interactive.get("type") == "list_reply":
            text = interactive["list_reply"].get("id", "")

    if not text:
        return

    print(f"Message reçu de {phone}: {text}")
    print(f"Élève: {user.name or 'Nouveau'} | Étape: {user.onboarding_step}")

    # Incrémente le compteur
    await user_service.increment_message_count(db, user)