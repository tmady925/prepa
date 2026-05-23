"""
Tâches planifiées — appelées par un cron job externe.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.core.settings import get_settings
from app.services.subscription_service import subscription_service

settings = get_settings()
router = APIRouter()


def verify_cron(x_cron_key: str = Header(None)):
    if x_cron_key != settings.admin_secret_key:
        raise HTTPException(status_code=401, detail="Non autorisé")
    return True


@router.post("/tasks/expire-subscriptions")
async def expire_subscriptions(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_cron),
):
    """Expire les abonnements Pro expirés. Appelé chaque heure."""
    count = await subscription_service.expire_subscriptions(db)
    return {"expired": count}


@router.post("/tasks/notify-expiring")
async def notify_expiring(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_cron),
):
    """Notifie les élèves dont l'abonnement expire dans 3 jours."""
    count = await subscription_service.notify_expiring_soon(db, days=3)
    return {"notified": count}


@router.post("/tasks/daily")
async def daily_tasks(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_cron),
):
    """Tâches quotidiennes — appelées chaque matin à 8h."""
    expired = await subscription_service.expire_subscriptions(db)
    notified = await subscription_service.notify_expiring_soon(db, days=3)
    return {
        "expired": expired,
        "notified": notified,
    }