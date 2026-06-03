"""
Service de gestion du profil cognitif des élèves.
Met à jour automatiquement le niveau de maîtrise après chaque échange.
"""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.exercise import StudentChapterMastery


class MasteryService:

    async def get_or_create(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
    ) -> StudentChapterMastery:
        """Récupère ou crée le profil de maîtrise pour un chapitre."""
        result = await db.execute(
            select(StudentChapterMastery).where(
                and_(
                    StudentChapterMastery.user_id == user_id,
                    StudentChapterMastery.matiere == matiere,
                    StudentChapterMastery.chapitre == chapitre,
                )
            )
        )
        mastery = result.scalar_one_or_none()

        if not mastery:
            mastery = StudentChapterMastery(
                user_id=user_id,
                matiere=matiere,
                chapitre=chapitre,
                mastery_level=0.1,
                types_vus=[],
                types_maitrises=[],
                types_faibles=[],
                weak_points=[],
            )
            db.add(mastery)
            await db.flush()

        return mastery

    async def update_after_interaction(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
        detection: dict,
        response_text: str,
        score: int = None,
    ) -> StudentChapterMastery:
        """Met à jour le profil après un échange."""
        if not matiere or not chapitre:
            return None

        mastery = await self.get_or_create(db, user_id, matiere, chapitre)
        now = datetime.now(ZoneInfo("Africa/Dakar"))

        mastery.practice_count += 1
        mastery.last_practiced_at = now

        type_demande = detection.get("type_demande", "cours")
        mots_cles = detection.get("mots_cles", [])
        niveau = detection.get("niveau_question", "intermediaire")

        if type_demande == "exercice":
            mastery.learning_style = "example_based"
        elif type_demande == "methode":
            mastery.learning_style = "formula_based"

        if score is not None:
            # Logique score numérique (après correction de copie)
            if score >= 70:
                delta = 0.10 + (score - 70) / 300  # 0.10 → 0.20 selon score
                mastery.mastery_level = min(1.0, mastery.mastery_level + delta)
                mastery.success_count += 1
            elif score >= 40:
                delta = 0.03 + (score - 40) / 1000  # 0.03 → 0.06
                mastery.mastery_level = min(1.0, mastery.mastery_level + delta)
            else:
                delta = max(-0.05, (score - 40) / 800)  # -0.05 → 0
                mastery.mastery_level = max(0.0, mastery.mastery_level + delta)
        else:
            # Ancienne logique — rétrocompatibilité
            if type_demande == "cours":
                mastery.mastery_level = max(0.1, mastery.mastery_level)
            elif type_demande == "exercice":
                mastery.mastery_level = min(0.8, mastery.mastery_level + 0.05)
            elif type_demande == "correction":
                mastery.mastery_level = min(0.9, mastery.mastery_level + 0.1)
                mastery.success_count += 1

            if niveau == "debutant" and mastery.mastery_level > 0.3:
                mastery.mastery_level = max(0.2, mastery.mastery_level - 0.05)

        if mots_cles and type_demande in ["cours", "methode"]:
            current_weak = mastery.weak_points or []
            for kw in mots_cles[:3]:
                if kw not in current_weak:
                    current_weak.append(kw)
            mastery.weak_points = current_weak[-10:]

        await db.flush()
        return mastery

    async def get_student_profile(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
    ) -> dict:
        """Retourne le profil complet d'un élève."""
        result = await db.execute(
            select(StudentChapterMastery).where(
                StudentChapterMastery.user_id == user_id
            ).order_by(StudentChapterMastery.matiere, StudentChapterMastery.chapitre)
        )
        masteries = result.scalars().all()

        profile = {}
        for m in masteries:
            if m.matiere not in profile:
                profile[m.matiere] = {}
            profile[m.matiere][m.chapitre] = {
                "level": round(m.mastery_level, 2),
                "practice_count": m.practice_count,
                "weak_points": m.weak_points or [],
                "last_practiced": m.last_practiced_at.isoformat() if m.last_practiced_at else None,
            }

        return profile

    async def get_chapter_context(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
    ) -> str:
        """Retourne le contexte de maîtrise pour injection dans le prompt."""
        result = await db.execute(
            select(StudentChapterMastery).where(
                and_(
                    StudentChapterMastery.user_id == user_id,
                    StudentChapterMastery.matiere == matiere,
                    StudentChapterMastery.chapitre == chapitre,
                )
            )
        )
        mastery = result.scalar_one_or_none()

        if not mastery:
            return ""

        level = mastery.mastery_level
        if level < 0.3:
            niveau_str = "débutant sur ce chapitre"
        elif level < 0.6:
            niveau_str = "niveau intermédiaire sur ce chapitre"
        else:
            niveau_str = "bon niveau sur ce chapitre"

        context = f"Niveau élève : {niveau_str} (score {level:.0%})."

        if mastery.weak_points:
            context += f" Points à renforcer : {', '.join(mastery.weak_points[:3])}."

        if mastery.practice_count > 0:
            context += f" A déjà travaillé ce chapitre {mastery.practice_count} fois."

        return context


mastery_service = MasteryService()