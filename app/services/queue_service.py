"""
Service de queue de notifications.

Principe :
- Si le user est dans un processus actif, les notifications sont stockées dans
  user.notification_queue (JSONB) pour envoi différé.
- Les offres emploi FREE sont aussi stockées avec send_after=+24h.
- flush_queue() envoie les messages dont send_after <= now (ou pas de send_after).
- Un cron /tasks/flush-delayed-notifications appelle flush_delayed_all() toutes les heures.
"""
from datetime import datetime, timezone, timedelta
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
    Envoie le message immédiatement si :
    - Le bloqueur de notifications est désactivé (config), OU
    - Le user n'est pas dans un processus actif.
    Sinon place le message dans notification_queue (JSONB) pour envoi différé.
    Retourne True si envoyé, False si mis en queue.
    """
    if not getattr(user, "phone_number", None):
        return False

    # Vérifie si le bloqueur est activé en config
    blocker_enabled = True
    try:
        from app.services.config_service import config_service
        blocker_enabled = await config_service.get_bool("notification_blocker_enabled")
    except Exception:
        pass  # par défaut : activé

    if not blocker_enabled or not is_busy(user):
        try:
            await whatsapp_sender.send_text(user.phone_number, message)
            return True
        except Exception as e:
            print(f"  ⚠️ send_or_queue erreur envoi {user.phone_number}: {e}")
            return False

    # User occupé + bloqueur actif → mise en queue
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


async def queue_delayed(db: AsyncSession, user: User, message: str, delay_hours: int = 24) -> None:
    """Place un message en queue avec envoi différé (delay_hours)."""
    if not getattr(user, "phone_number", None):
        return
    send_after = (datetime.now(timezone.utc) + timedelta(hours=delay_hours)).isoformat()
    queue = list(user.notification_queue or [])
    queue.append({
        "message": message,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "send_after": send_after,
    })
    user.notification_queue = queue
    await db.flush()
    print(f"  ⏳ Message différé +{delay_hours}h → {user.phone_number} (send_after={send_after[:16]})")


async def flush_delayed_all(db: AsyncSession) -> int:
    """
    Parcourt tous les users avec des messages en queue dont send_after <= now.
    Appelé par le cron /tasks/flush-delayed-notifications (toutes les heures).
    """
    from sqlalchemy import select, cast
    from sqlalchemy.dialects.postgresql import JSONB
    from app.models.user import User

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(User).where(User.notification_queue != [])
    )
    users = result.scalars().all()

    total_sent = 0
    for user in users:
        queue = list(user.notification_queue or [])
        remaining = []
        for item in queue:
            send_after_str = item.get("send_after")
            if send_after_str:
                try:
                    send_after = datetime.fromisoformat(send_after_str)
                    if send_after.tzinfo is None:
                        send_after = send_after.replace(tzinfo=timezone.utc)
                    if send_after > now:
                        remaining.append(item)
                        continue
                except Exception:
                    pass
            # Pas de send_after ou délai écoulé → envoyer
            try:
                await whatsapp_sender.send_text(user.phone_number, item["message"])
                total_sent += 1
            except Exception as e:
                print(f"  ⚠️ flush_delayed erreur {user.phone_number}: {e}")
                remaining.append(item)

        if len(remaining) != len(queue):
            user.notification_queue = remaining
            await db.flush()

    if total_sent:
        print(f"  📬 flush_delayed_all: {total_sent} message(s) envoyé(s)")
    return total_sent
