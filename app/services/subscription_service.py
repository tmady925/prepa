"""
Gestion des abonnements — expiration, renouvellement, notifications.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.subscription import Subscription
from app.models.user import User
from app.services.whatsapp.sender import whatsapp_sender


class SubscriptionService:

    async def expire_subscriptions(self, db: AsyncSession) -> int:
        """
        Vérifie et expire les abonnements expirés.
        Retourne le nombre d'abonnements expirés.
        """
        now = datetime.now(ZoneInfo("Africa/Dakar"))

        # Trouve les abonnements actifs expirés
        result = await db.execute(
            select(Subscription).where(
                and_(
                    Subscription.status == "active",
                    Subscription.expires_at <= now,
                )
            )
        )
        expired = result.scalars().all()

        count = 0
        for sub in expired:
            # Expire l'abonnement
            sub.status = "expired"

            # Repasse l'utilisateur en free
            user_result = await db.execute(
                select(User).where(User.id == sub.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user and user.plan == "pro":
                user.plan = "free"
                count += 1

                # Notifie l'élève
                await self._notify_expiration(user)

        if count > 0:
            await db.commit()
            print(f"Abonnements expirés: {count}")

        return count

    async def notify_expiring_soon(self, db: AsyncSession, days: int = 3) -> int:
        """
        Notifie les élèves dont l'abonnement expire dans X jours.
        """
        from datetime import timedelta
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        deadline = now + timedelta(days=days)

        result = await db.execute(
            select(Subscription, User)
            .join(User, User.id == Subscription.user_id)
            .where(
                and_(
                    Subscription.status == "active",
                    Subscription.expires_at <= deadline,
                    Subscription.expires_at > now,
                )
            )
        )
        rows = result.all()

        count = 0
        for sub, user in rows:
            days_left = (sub.expires_at - now).days
            await whatsapp_sender.send_text(
                user.phone_number,
                f"⏰ Ton abonnement *Prepa Pro* expire dans *{days_left} jours*.\n\n"
                f"Renouvelle maintenant pour continuer à réviser sans limite !\n\n"
                f"Tape */plan* pour renouveler. 💪"
            )
            count += 1

        return count

    async def _notify_expiration(self, user: User) -> None:
        """Notifie l'élève que son abonnement a expiré."""
        try:
            await whatsapp_sender.send_text(
                user.phone_number,
                f"😔 Ton abonnement *Prepa Pro* a expiré *{user.name}*.\n\n"
                f"Tu es repassé en version gratuite.\n\n"
                f"Tape */plan* pour renouveler à *500 FCFA/mois* 🔄"
            )
        except Exception as e:
            print(f"Erreur notification expiration: {e}")


subscription_service = SubscriptionService()