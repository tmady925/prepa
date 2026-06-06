import uuid
import random
import string
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.services.config_service import config_service


def generate_referral_code(name: str = "") -> str:
    prefix = name[:3].upper() if name else "PRE"
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{suffix}"


class UserService:

    async def get_by_phone(self, db: AsyncSession, phone: str) -> User | None:
        result = await db.execute(
            select(User).where(User.phone_number == phone)
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, db: AsyncSession, phone: str) -> tuple[User, bool]:
        user = await self.get_by_phone(db, phone)
        if user:
            return user, False

        user = User(
            phone_number=phone,
            status="onboarding",
            onboarding_step="start",
            plan="free",
        )
        db.add(user)
        await db.flush()
        return user, True

    async def set_name(self, db: AsyncSession, user: User, name: str) -> User:
        # L'étape suivante (confirm_pays / saisie_pays) est définie par le handler appelant
        user.name = name
        user.referral_code = generate_referral_code(name)
        await db.flush()
        return user

    async def set_exam(self, db: AsyncSession, user: User, exam_type: str) -> User:
        user.exam_type = exam_type
        user.onboarding_step = "series"
        await db.flush()
        return user

    async def set_series(self, db: AsyncSession, user: User, series: str) -> User:
        user.series = series
        user.onboarding_step = "subjects"
        await db.flush()
        return user

    async def set_subjects(self, db: AsyncSession, user: User, subjects: list) -> User:
        user.subjects = subjects
        user.onboarding_step = "exam_date"
        await db.flush()
        return user

    async def set_exam_date(self, db: AsyncSession, user: User, exam_date: datetime) -> User:
        user.exam_date = exam_date
        user.onboarding_step = "plan"
        await db.flush()
        return user

    async def complete_onboarding(self, db: AsyncSession, user: User) -> User:
        user.onboarding_step = "done"
        user.status = "active"
        # L'onboarding ne doit pas consommer le quota journalier :
        # on repart d'un quota plein pour le premier vrai jour d'utilisation.
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        user.daily_messages_used = 0
        user.quota_reset_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        await db.flush()
        return user

    async def check_quota(self, user: User) -> dict:
        daily_limit = await config_service.get_int("free_messages_per_day")

        if user.plan == "pro":
            return {"allowed": True, "used": user.daily_messages_used, "limit": -1, "bonus": 0}

        total_limit = daily_limit + user.daily_messages_bonus
        allowed = user.daily_messages_used < total_limit

        return {
            "allowed": allowed,
            "used": user.daily_messages_used,
            "limit": total_limit,
            "bonus": user.daily_messages_bonus,
        }

    async def increment_message_count(self, db: AsyncSession, user: User) -> User:
        now = datetime.now(ZoneInfo("Africa/Dakar"))

        if user.quota_reset_at is None or now >= user.quota_reset_at:
            user.daily_messages_used = 0
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            user.quota_reset_at = tomorrow

        user.daily_messages_used += 1
        user.total_messages += 1

        await self._update_streak(db, user, now)
        await self._update_engagement_score(user)

        # Vérifie le parrainage à chaque message
        from app.services.referral_service import referral_service
        await referral_service.check_and_activate(db, user)

        await db.flush()
        return user

    async def _update_streak(self, db: AsyncSession, user: User, now: datetime) -> None:
        today = now.date()

        if user.streak_last_active is None:
            user.streak_days = 1
        else:
            last = user.streak_last_active.date()
            delta = (today - last).days
            if delta == 0:
                pass
            elif delta == 1:
                user.streak_days += 1
            else:
                user.streak_days = 1

        user.streak_last_active = now

    async def _update_engagement_score(self, user: User) -> None:
        score = 0

        if user.streak_days >= 5:         score += 25
        if user.streak_days >= 3:         score += 10
        if user.total_messages >= 20:     score += 10
        if user.daily_messages_used >= 8: score += 20

        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = (exam_date - datetime.now()).days
            if days_left <= 30: score += 25
            if days_left <= 7:  score += 15

        if user.upsell_refused_count > 0:
            score -= user.upsell_refused_count * 10

        user.engagement_score = max(0, min(100, score))

    async def should_show_upsell(self, user: User) -> bool:
        if user.plan != "free":
            return False

        threshold = await config_service.get_int("upsell_engagement_threshold")
        cooldown = await config_service.get_int("upsell_cooldown_days")

        if user.engagement_score < threshold:
            return False

        if user.last_upsell_shown_at:
            days_since = (
                datetime.now(ZoneInfo("Africa/Dakar")) - user.last_upsell_shown_at
            ).days
            if days_since < cooldown:
                return False

        return True

    async def apply_referral(
        self, db: AsyncSession, new_user: User, referral_code: str
    ) -> bool:
        """
        Associe un parrain au nouvel élève via son code.
        Le code peut être le code brut (MAD-JAIQQ) ou avec préfixe (PREPA-MAD-JAIQQ).
        """
        # Nettoie le code — supprime le préfixe PREPA- si présent
        code = referral_code.upper().strip()
        if code.startswith("PREPA-"):
            code = code[6:]

        result = await db.execute(
            select(User).where(User.referral_code == code)
        )
        referrer = result.scalar_one_or_none()

        if not referrer or referrer.id == new_user.id:
            return False

        # Vérifie que le parrainage n'existe pas déjà
        if new_user.referred_by_id:
            return False

        from app.models.subscription import Referral
        referral = Referral(
            referrer_id=referrer.id,
            referred_id=new_user.id,
            status="pending",
        )
        db.add(referral)
        new_user.referred_by_id = referrer.id
        await db.flush()
        return True


user_service = UserService()