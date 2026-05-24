"""
Service de notifications manuelles et automatiques.
Géré depuis le dashboard admin.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.services.whatsapp.sender import whatsapp_sender


class NotificationService:

    async def get_targets(
        self,
        db: AsyncSession,
        filter_type: str = "all",
        exam_type: str = None,
        series: str = None,
    ) -> list[User]:
        """Récupère les élèves selon les filtres."""
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        filters = [
            User.status == "active",
            User.onboarding_step == "done",
        ]

        if filter_type == "inactive_24h":
            filters.append(User.streak_last_active < now - timedelta(hours=24))
        elif filter_type == "inactive_7d":
            filters.append(User.streak_last_active < now - timedelta(days=7))
        elif filter_type == "before_exam_7d":
            deadline = now + timedelta(days=7)
            filters.append(User.exam_date <= deadline.replace(tzinfo=None))
            filters.append(User.exam_date >= now.replace(tzinfo=None))
        elif filter_type == "before_exam_30d":
            deadline = now + timedelta(days=30)
            filters.append(User.exam_date <= deadline.replace(tzinfo=None))
            filters.append(User.exam_date >= now.replace(tzinfo=None))
        elif filter_type == "free_users":
            filters.append(User.plan == "free")
        elif filter_type == "pro_users":
            filters.append(User.plan == "pro")

        if exam_type:
            filters.append(User.exam_type == exam_type)
        if series:
            filters.append(User.series == series)

        result = await db.execute(select(User).where(and_(*filters)))
        return result.scalars().all()

    async def count_targets(
        self,
        db: AsyncSession,
        filter_type: str = "all",
        exam_type: str = None,
        series: str = None,
    ) -> int:
        users = await self.get_targets(db, filter_type, exam_type, series)
        return len(users)

    async def send_campaign(
        self,
        db: AsyncSession,
        filter_type: str,
        message_type: str,
        custom_message: str = None,
        exam_type: str = None,
        series: str = None,
    ) -> dict:
        """Envoie une campagne de notifications."""
        users = await self.get_targets(db, filter_type, exam_type, series)

        sent = 0
        failed = 0

        for user in users:
            try:
                if custom_message:
                    msg = self._personalize(custom_message, user)
                else:
                    msg = await self._build_message(user, message_type)

                await whatsapp_sender.send_text(user.phone_number, msg)
                sent += 1
            except Exception as e:
                print(f"Erreur notification {user.phone_number}: {e}")
                failed += 1

        return {
            "total_targets": len(users),
            "sent": sent,
            "failed": failed,
        }

    def _personalize(self, message: str, user: User) -> str:
        """Remplace les variables dans un message personnalisé."""
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        days_left = 0
        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = max(0, (exam_date - now.replace(tzinfo=None)).days)

        return (
            message
            .replace("{nom}", user.name or "ami")
            .replace("{streak}", str(user.streak_days))
            .replace("{jours_restants}", str(days_left))
            .replace("{plan}", user.plan.upper())
            .replace("{exam}", user.exam_type or "examen")
            .replace("{serie}", user.series or "")
            .replace("{messages}", str(user.total_messages))
        )

    async def _build_message(self, user: User, message_type: str) -> str:
        """Construit un message prédéfini selon le type."""
        now = datetime.now(ZoneInfo("Africa/Dakar"))
        name = user.name or "ami"
        days_left = 0
        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = max(0, (exam_date - now.replace(tzinfo=None)).days)

        if message_type == "motivation":
            return (
                f"🔥 *{name}*, ton streak de *{user.streak_days} jours* est impressionnant !\n\n"
                f"Continue comme ça — il te reste *{days_left} jours* avant ton examen. 💪\n\n"
                f"Pose-moi une question pour continuer ta révision !"
            )
        elif message_type == "rappel":
            return (
                f"📚 Salut *{name}* !\n\n"
                f"N'oublie pas de réviser aujourd'hui.\n\n"
                f"Il te reste *{days_left} jours* avant ton examen.\n\n"
                f"Tape */profil* pour voir ta progression 📊"
            )
        elif message_type == "urgence":
            return (
                f"⚠️ *{name}* — Plus que *{days_left} jours* avant ton examen !\n\n"
                f"C'est le moment de tout donner. Dis-moi sur quoi tu veux travailler 🎯"
            )
        elif message_type == "upsell":
            return (
                f"⭐ *{name}*, passe *Prepa Pro* et révise sans limite !\n\n"
                f"✅ Messages illimités\n✅ Réponses plus détaillées\n\n"
                f"💰 Seulement *500 FCFA/mois*\n\nTape */plan* 🚀"
            )
        elif message_type == "weekend":
            return (
                f"🌟 Bon weekend *{name}* !\n\n"
                f"*{days_left} jours* avant ton examen — un peu de révision fait toute la différence !\n\n"
                f"Je suis disponible 24h/24 📚"
            )

        return (
            f"👋 Salut *{name}* !\n\n"
            f"Je suis Prepa, ton assistant de révision.\n"
            f"Pose-moi une question pour continuer ta préparation ! 📚"
        )


notification_service = NotificationService()