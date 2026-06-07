"""
Service de queue de notifications.

Principe :
- Si le user est dans un processus actif (exercice, correction, simulation),
  les notifications (offres emploi, campagnes admin, résultats simulation...)
  sont stockées dans user.notification_queue (JSONB).
- Dès que le user sort du processus, flush_queue() envoie tous les messages
  en attente dans l'ordre.

Processus "occupé" détectés via conversation_state :
  - awaiting_simulation_copy  → simulation en cours
  - awaiting_copy             → correction exercice en attente
  - exercise_path             → exercice en cours
  - awaiting_copy_for_free_correction → correction libre en attente
"""
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.services.whatsapp.sender import whatsapp_sender


def is_busy(user: User) -> bool:
    """Retourne True si le user est dans un processus actif."""
    conv = user.conversation_state or {}
    return bool(
        conv.get("awaiting_simulation_copy") or
        conv.get("awaiting_copy") or
        conv.get("exercise_path") or
        conv.get("awaiting_copy_for_free_correction")
    )


async def send_or_queue(db: AsyncSession, user: User, message: str) -> bool:
    """
    Envoie le message immédiatement si le user est libre.
    Sinon le place dans notification_queue pour envoi différé.
    Retourne True si envoyé, False si mis en queue.
    """
    if not getattr(user, "phone_number", None):
        return False

    if not is_busy(user):
        try:
            await whatsapp_sender.send_text(user.phone_number, message)
            return True
        except Exception as e:
            print(f"  ⚠️ send_or_queue erreur envoi {user.phone_number}: {e}")
            return False

    # User occupé → mise en queue
    queue = list(user.notification_queue or [])
    queue.append({
        "message": message,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    })
    user.notification_queue = queue
    await db.flush()
    print(f"  📬 Notification mise en queue pour {user.phone_number} ({len(queue)} en attente)")
    return False


async def flush_queue(db: AsyncSession, user: User) -> int:
    """
    Envoie tous les messages en attente dans la queue et la vide.
    À appeler dès que le user sort d'un processus.
    Retourne le nombre de messages envoyés.
    """
    queue = list(user.notification_queue or [])
    if not queue:
        return 0

    sent = 0
    for item in queue:
        try:
            await whatsapp_sender.send_text(user.phone_number, item["message"])
            sent += 1
        except Exception as e:
            print(f"  ⚠️ flush_queue erreur {user.phone_number}: {e}")

    user.notification_queue = []
    await db.flush()

    if sent:
        print(f"  📬 Queue flushée pour {user.phone_number} : {sent}/{len(queue)} message(s) envoyé(s)")
    return sent
