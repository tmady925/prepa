from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.subscription import Referral
from app.models.user import User
from app.services.config_service import config_service


class ReferralService:

    async def check_and_activate(self, db: AsyncSession, user: User) -> bool:
        """
        Appelé à chaque message d'un élève.
        Vérifie si cet élève est un filleul qui vient d'atteindre le seuil d'activation.
        Si oui, active le parrainage et crédite le parrain.
        Retourne True si un parrainage vient d'être activé.
        """
        if not user.referred_by_id:
            return False

        # Cherche un parrainage pending pour cet élève
        result = await db.execute(
            select(Referral).where(
                Referral.referred_id == user.id,
                Referral.status == "pending",
            )
        )
        referral = result.scalar_one_or_none()
        if not referral:
            return False

        # Vérifie les critères d'activation
        min_msgs = await config_service.get_int("referral_min_msgs")
        active_days = await config_service.get_int("referral_active_days")

        if user.total_messages < min_msgs:
            return False

        if not user.streak_last_active:
            return False

        # Vérifie que l'élève a été actif dans les N derniers jours
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        days_since_signup = (now - user.created_at).days
        if days_since_signup > active_days:
            return False

        # Active le parrainage
        referral.status = "active"
        referral.activated_at = now
        referral.bonus_messages_granted = await config_service.get_int("referral_bonus_messages")

        # Crédite le parrain
        result = await db.execute(
            select(User).where(User.id == user.referred_by_id)
        )
        referrer = result.scalar_one_or_none()
        if referrer:
            bonus = await config_service.get_int("referral_bonus_messages")
            referrer.daily_messages_bonus += bonus
            print(f"Parrainage activé: {referrer.phone_number} gagne {bonus} messages")

        await db.flush()
        return True

    async def activate_paid_bonus(self, db: AsyncSession, user: User) -> bool:
        """
        Appelé quand un filleul passe Pro.
        Crédite le parrain d'un bonus supplémentaire.
        """
        if not user.referred_by_id:
            return False

        result = await db.execute(
            select(Referral).where(
                Referral.referred_id == user.id,
                Referral.status.in_(["pending", "active"]),
            )
        )
        referral = result.scalar_one_or_none()
        if not referral:
            return False

        referral.status = "paid"

        result = await db.execute(
            select(User).where(User.id == user.referred_by_id)
        )
        referrer = result.scalar_one_or_none()
        if referrer:
            bonus = await config_service.get_int("referral_paid_bonus")
            referrer.daily_messages_bonus += bonus
            print(f"Bonus payant: {referrer.phone_number} gagne {bonus} messages")

        await db.flush()
        return True


referral_service = ReferralService()