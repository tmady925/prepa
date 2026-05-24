"""
Générateur d'exercices adaptatifs avec diagnostic caché.
- Commence niveau intermédiaire/avancé par défaut
- Analyse silencieuse des réponses
- Adaptation progressive du niveau
"""
import json
import re
import httpx
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.exercise import ExerciseType, StudentChapterMastery
from app.core.settings import get_settings

settings = get_settings()


class ExerciseGenerator:

    # ── Génération d'exercice ─────────────────────────────────────────

    async def generate_exercise(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
        exam_type: str = "bac_senegal",
        serie: str = "S2",
    ) -> dict:
        """
        Génère un exercice adapté au niveau réel de l'élève.
        Par défaut : niveau intermédiaire/avancé (diagnostic caché).
        Retourne : {text, type, chapitre, niveau, correct_answer_hint}
        """
        # Récupère le profil
        mastery = await self._get_mastery(db, user_id, matiere, chapitre)
        niveau = self._estimate_level(mastery)

        # Récupère les types d'exercices
        exercise_type = await self._pick_exercise_type(db, matiere, chapitre, mastery)

        # Génère via LLM
        exercise = await self._generate_with_llm(
            matiere=matiere,
            chapitre=chapitre,
            niveau=niveau,
            exercise_type=exercise_type,
            exam_type=exam_type,
            serie=serie,
        )

        return {
            "text": exercise["enonce"],
            "type": exercise_type.slug if exercise_type else "general",
            "type_nom": exercise_type.nom if exercise_type else chapitre,
            "chapitre": chapitre,
            "matiere": matiere,
            "niveau": niveau,
            "answer_hint": exercise.get("reponse_attendue", ""),
            "methode": exercise.get("methode", ""),
        }

    # ── Analyse de la réponse de l'élève ─────────────────────────────

    async def analyze_answer(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
        exercise_text: str,
        student_answer: str,
        exercise_type: str = "",
        hints_asked: int = 0,
    ) -> dict:
        """
        Analyse silencieuse de la réponse de l'élève.
        Met à jour le profil cognitif.
        Retourne la correction + évaluation.
        """
        # Analyse via LLM
        analysis = await self._analyze_with_llm(
            exercise_text=exercise_text,
            student_answer=student_answer,
            matiere=matiere,
            chapitre=chapitre,
        )

        # Met à jour le profil selon la performance
        await self._update_mastery(
            db=db,
            user_id=user_id,
            matiere=matiere,
            chapitre=chapitre,
            is_correct=analysis.get("correct", False),
            method_used=analysis.get("methode_utilisee", ""),
            errors=analysis.get("erreurs", []),
            hints_asked=hints_asked,
            exercise_type=exercise_type,
        )

        return analysis

    # ── Estimation du niveau ──────────────────────────────────────────

    def _estimate_level(self, mastery: StudentChapterMastery | None) -> str:
        """
        Estime le niveau de l'élève.
        Par défaut : intermediaire (diagnostic caché).
        """
        if not mastery or mastery.practice_count == 0:
            # Première interaction → commence intermédiaire/avancé
            # C'est le diagnostic caché
            return "intermediaire_avance"

        level = mastery.mastery_level
        hints_ratio = 0
        if mastery.practice_count > 0:
            # Si l'élève demande souvent des indices → niveau bas
            pass

        if level >= 0.75:
            return "avance"
        elif level >= 0.45:
            return "intermediaire"
        elif level >= 0.2:
            return "debutant_intermediaire"
        else:
            return "debutant"

    async def _pick_exercise_type(
        self,
        db: AsyncSession,
        matiere: str,
        chapitre: str,
        mastery: StudentChapterMastery | None,
    ) -> ExerciseType | None:
        """Choisit le type d'exercice adapté."""
        result = await db.execute(
            select(ExerciseType).where(
                and_(
                    ExerciseType.matiere == matiere,
                    ExerciseType.chapitre == chapitre,
                    ExerciseType.is_active == True,
                )
            ).order_by(ExerciseType.difficulte)
        )
        types = result.scalars().all()

        if not types:
            return None

        if not mastery or mastery.practice_count == 0:
            # Diagnostic → exercice de difficulté moyenne (type BAC)
            medium = [t for t in types if t.difficulte == 2]
            if medium:
                return medium[0]
            return types[len(types) // 2]

        types_vus = mastery.types_vus or []
        types_maitrises = mastery.types_maitrises or []

        # Priorité : types pas encore vus
        not_seen = [t for t in types if t.slug not in types_vus]
        if not_seen:
            return not_seen[0]

        # Types vus mais pas maîtrisés
        not_mastered = [t for t in types if t.slug not in types_maitrises]
        if not_mastered:
            return not_mastered[0]

        # Tout maîtrisé → exercice difficile
        hard = [t for t in types if t.difficulte == 3]
        return hard[0] if hard else types[-1]

    # ── Génération LLM ────────────────────────────────────────────────

    async def _generate_with_llm(
        self,
        matiere: str,
        chapitre: str,
        niveau: str,
        exercise_type: ExerciseType | None,
        exam_type: str,
        serie: str,
    ) -> dict:
        """Génère l'exercice via Mistral."""

        niveau_instructions = {
            "intermediaire_avance": (
                "Niveau intermédiaire/avancé. Génère un exercice TYPE BAC — "
                "directement utilisable en examen. Pas trop simple. "
                "C'est un diagnostic caché : on veut voir ce que l'élève sait faire."
            ),
            "avance": (
                "Niveau avancé. Exercice difficile, multi-étapes, comme une vraie question "
                "de BAC. Données réalistes, calculs non triviaux."
            ),
            "intermediaire": (
                "Niveau intermédiaire. Exercice standard avec 2-3 questions progressives. "
                "Données claires, une seule difficulté principale."
            ),
            "debutant_intermediaire": (
                "Niveau débutant/intermédiaire. Exercice guidé avec questions décomposées. "
                "Rappelle la formule à utiliser dans l'énoncé."
            ),
            "debutant": (
                "Niveau débutant. Exercice très guidé, application directe d'une formule. "
                "Étapes clairement indiquées."
            ),
        }

        type_context = ""
        if exercise_type:
            type_context = f"\nType d'exercice : {exercise_type.nom}"
            if exercise_type.methode_steps:
                steps = [s["action"] for s in exercise_type.methode_steps[:4]]
                type_context += f"\nMéthode : {' → '.join(steps)}"
            if exercise_type.formules_cles:
                type_context += f"\nFormules clés : {', '.join(exercise_type.formules_cles[:3])}"

        prompt = f"""Génère un exercice de {matiere} sur "{chapitre.replace('_', ' ')}" pour le {exam_type} {serie} au Sénégal.

{niveau_instructions.get(niveau, niveau_instructions['intermediaire'])}
{type_context}

IMPORTANT :
- Ne donne PAS la correction dans l'énoncé
- Utilise des données réalistes (contexte sénégalais si possible)
- L'exercice doit être auto-suffisant

Réponds UNIQUEMENT avec ce JSON valide :
{{
  "enonce": "Texte complet de l'exercice avec données et questions numérotées. Termine par 'Envoie ta réponse quand tu es prêt ! 💪'",
  "reponse_attendue": "Description courte de ce qu'on attend (pour usage interne, pas affiché)",
  "methode": "Méthode principale à appliquer",
  "points_cles": ["point 1", "point 2"]
}}"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 600,
                        "temperature": 0.7,
                    },
                )
                data = response.json()
                raw = data["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        except Exception as e:
            print(f"Exercise generation error: {e}")

        # Fallback
        return {
            "enonce": f"*Exercice — {chapitre.replace('_', ' ').title()}*\n\nEnvoie ta réponse quand tu es prêt ! 💪",
            "reponse_attendue": "",
            "methode": "",
        }

    # ── Analyse LLM ───────────────────────────────────────────────────

    async def _analyze_with_llm(
        self,
        exercise_text: str,
        student_answer: str,
        matiere: str,
        chapitre: str,
    ) -> dict:
        """Analyse la réponse de l'élève via LLM."""

        prompt = f"""Tu corriges la réponse d'un élève sénégalais en {matiere} (chapitre: {chapitre}).

Exercice :
{exercise_text[:500]}

Réponse de l'élève :
{student_answer}

Analyse la réponse et réponds UNIQUEMENT avec ce JSON :
{{
  "correct": true ou false,
  "score": 0 à 100,
  "methode_utilisee": "description de la méthode utilisée par l'élève",
  "erreurs": ["erreur 1 détectée", "erreur 2"],
  "points_forts": ["ce que l'élève a bien fait"],
  "correction": "Correction complète étape par étape",
  "encouragement": "Message motivant adapté au résultat",
  "niveau_estime": "debutant ou intermediaire ou avance",
  "prochain_conseil": "Ce sur quoi l'élève devrait travailler ensuite"
}}"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 800,
                        "temperature": 0.2,
                    },
                )
                data = response.json()
                raw = data["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        except Exception as e:
            print(f"Answer analysis error: {e}")

        return {
            "correct": False,
            "score": 0,
            "correction": "Je n'ai pas pu analyser ta réponse. Réessaie.",
            "encouragement": "Continue d'essayer ! 💪",
            "niveau_estime": "intermediaire",
        }

    # ── Mise à jour profil ────────────────────────────────────────────

    async def _get_mastery(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
    ) -> StudentChapterMastery | None:
        result = await db.execute(
            select(StudentChapterMastery).where(
                and_(
                    StudentChapterMastery.user_id == user_id,
                    StudentChapterMastery.matiere == matiere,
                    StudentChapterMastery.chapitre == chapitre,
                )
            )
        )
        return result.scalar_one_or_none()

    async def _update_mastery(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        matiere: str,
        chapitre: str,
        is_correct: bool,
        method_used: str,
        errors: list,
        hints_asked: int,
        exercise_type: str,
    ) -> None:
        """Met à jour le profil cognitif selon la performance."""
        from app.services.rag.mastery_service import mastery_service
        mastery = await mastery_service.get_or_create(db, user_id, matiere, chapitre)
        now = datetime.now(ZoneInfo("Africa/Dakar"))

        mastery.practice_count += 1
        mastery.last_practiced_at = now

        # Ajuste le niveau selon la performance
        if is_correct and hints_asked == 0:
            # Réussi sans aide → monte le niveau
            mastery.mastery_level = min(1.0, mastery.mastery_level + 0.15)
            mastery.success_count += 1
            # Marque le type comme maîtrisé
            if exercise_type:
                types_maitrises = mastery.types_maitrises or []
                if exercise_type not in types_maitrises:
                    types_maitrises.append(exercise_type)
                mastery.types_maitrises = types_maitrises
        elif is_correct and hints_asked > 0:
            # Réussi avec aide → monte légèrement
            mastery.mastery_level = min(1.0, mastery.mastery_level + 0.05)
            mastery.success_count += 1
        elif not is_correct and hints_asked == 0:
            # Raté sans aide → baisse légèrement
            mastery.mastery_level = max(0.0, mastery.mastery_level - 0.05)
        else:
            # Raté avec aide → baisse plus
            mastery.mastery_level = max(0.0, mastery.mastery_level - 0.1)

        # Enregistre les erreurs détectées
        if errors:
            current_weak = mastery.weak_points or []
            for err in errors[:2]:
                if err not in current_weak:
                    current_weak.append(err)
            mastery.weak_points = current_weak[-8:]

        # Marque le type comme vu
        if exercise_type:
            types_vus = mastery.types_vus or []
            if exercise_type not in types_vus:
                types_vus.append(exercise_type)
            mastery.types_vus = types_vus

        await db.flush()


exercise_generator = ExerciseGenerator()