"""
Service Petit Job
=================
Gère la publication, la recherche et la notification des petits jobs
postés directement via WhatsApp par les employeurs.
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.models.petit_job import PetitJob
from app.models.user import User

settings = get_settings()

EXTRACT_SYSTEM = """Tu es un assistant qui extrait des informations structurées d'annonces de petits jobs en Afrique.
Réponds UNIQUEMENT avec du JSON valide, rien d'autre."""

EXTRACT_PROMPT = """Extrait les informations de cette annonce de petit job :
MESSAGE : "{text}"

Réponds avec ce JSON (null si information absente) :
{{
  "titre": "...",
  "type_travail": "livraison|manutention|vente|événementiel|nettoyage|gardiennage|autre",
  "lieu": "...",
  "date_debut_str": "...",
  "duree": "...",
  "remuneration": "...",
  "nb_postes": 1,
  "is_job_offer": true
}}

Si le message ne ressemble pas à une offre de travail, mets is_job_offer à false."""


class PetitJobService:

    async def extract_from_text(self, text: str, user) -> dict | None:
        """Extrait les champs d'un petit job depuis un message libre via LLM."""
        prompt = EXTRACT_PROMPT.format(text=text.replace('"', "'"))
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ]

        providers = []
        if getattr(settings, "groq_api_key", None):
            providers.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                              "llama-3.3-70b-versatile", settings.groq_api_key))
        if getattr(settings, "mistral_api_key", None):
            providers.append(("mistral", "https://api.mistral.ai/v1/chat/completions",
                              "mistral-small-latest", settings.mistral_api_key))

        for name, url, model, key in providers:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": messages, "max_tokens": 300, "temperature": 0.1},
                    )
                    if resp.status_code != 200:
                        continue
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    raw = re.sub(r"^```(?:json)?\s*", "", raw)
                    raw = re.sub(r"\s*```$", "", raw)
                    data = json.loads(raw)
                    if not data.get("is_job_offer", True):
                        return None
                    # Fallback titre depuis le texte si absent
                    if not data.get("titre"):
                        data["titre"] = text[:80].strip()
                    data["description"] = text
                    # Localisation employeur en fallback
                    if not data.get("lieu") and getattr(user, "localisation_emploi", None):
                        data["lieu"] = user.localisation_emploi
                    return data
            except Exception as e:
                print(f"  [petit_job] extract provider {name} error: {e}")
                continue

        return None

    async def create(self, db: AsyncSession, employeur_id: uuid.UUID, draft: dict) -> PetitJob:
        """Crée un petit job et le persiste en base."""
        expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
        job = PetitJob(
            employeur_user_id=employeur_id,
            titre=draft.get("titre", "Petit job"),
            description=draft.get("description"),
            type_travail=draft.get("type_travail"),
            lieu=draft.get("lieu"),
            date_debut_str=draft.get("date_debut_str"),
            duree=draft.get("duree"),
            remuneration=draft.get("remuneration"),
            nb_postes=int(draft.get("nb_postes") or 1),
            statut="ouvert",
            expires_at=expires_at,
        )
        db.add(job)
        await db.flush()
        return job

    async def list_active(self, db: AsyncSession, lieu: str | None = None) -> list[PetitJob]:
        """Liste les petits jobs ouverts, filtrés optionnellement par lieu."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(PetitJob)
            .where(
                PetitJob.statut == "ouvert",
                PetitJob.expires_at > now,
            )
            .order_by(PetitJob.created_at.desc())
            .limit(10)
        )
        result = await db.execute(stmt)
        jobs = result.scalars().all()

        if lieu and jobs:
            lieu_lower = lieu.lower()
            local = [j for j in jobs if j.lieu and lieu_lower in j.lieu.lower()]
            if local:
                return local

        return jobs

    # ── Scoring ────────────────────────────────────────────────────────────────

    # Correspondances type_travail → secteurs emploi (pour le score type)
    _TYPE_TO_SECTEURS: dict[str, list[str]] = {
        "livraison":      ["Informatique/Tech", "Marketing/Communication"],
        "manutention":    ["BTP/Ingénierie"],
        "vente":          ["Marketing/Communication", "Finance/Comptabilité"],
        "événementiel":   ["Marketing/Communication"],
        "nettoyage":      [],
        "gardiennage":    [],
        "autre":          [],
    }

    def _score_candidate(
        self,
        job: "PetitJob",
        user: User,
        emploi_type: str | None,
    ) -> float:
        """
        Score 0–100 d'un candidat pour un petit job.
        Pondération :
          - emploi_type preference : 30 pts
          - correspondance géographique  : 40 pts
          - correspondance type_travail  : 30 pts
        """
        score = 0.0

        # ── 1. emploi_type preference (30 pts) ──────────────────────────────
        if emploi_type == "petit_job":
            score += 30
        elif emploi_type == "les_deux":
            score += 20
        elif emploi_type == "entreprise":
            return 0.0  # exclusion stricte — candidat orienté entreprise
        else:
            score += 10  # pas encore déterminé → partiel

        # ── 2. Géolocalisation (40 pts) ─────────────────────────────────────
        user_loc = (getattr(user, "localisation_emploi", None) or "").lower().strip()
        job_loc = (job.lieu or "").lower().strip()

        if not user_loc or not job_loc:
            score += 15  # info manquante → partiel
        elif user_loc in job_loc or job_loc in user_loc:
            score += 40  # match exact
        else:
            # Essaie une correspondance partielle (ex: "Dakar" dans "Dakar Plateau")
            user_words = set(user_loc.split())
            job_words = set(job_loc.split())
            if user_words & job_words:
                score += 25
            else:
                return 0.0  # villes différentes → n'envoie pas

        # ── 3. Type de travail vs secteur (30 pts) ──────────────────────────
        job_type = (job.type_travail or "").lower()
        user_secteurs = getattr(user, "secteur_emploi", None) or []
        if isinstance(user_secteurs, str):
            user_secteurs = [user_secteurs]
        user_secteurs_lower = [s.lower() for s in user_secteurs]

        # Match direct : si le user travaille dans un secteur lié à ce type de job
        related_sectors = [s.lower() for s in self._TYPE_TO_SECTEURS.get(job_type, [])]
        if related_sectors:
            if any(s in " ".join(user_secteurs_lower) for s in related_sectors):
                score += 30
            else:
                score += 10
        else:
            # Pas d'info secteur ou type inconnu → partiel
            score += 15

        return score

    async def notify_candidates(self, db: AsyncSession, job: PetitJob) -> int:
        """
        Notifie les candidats par score décroissant.
        Score = géo (40%) + emploi_type (30%) + type_travail (30%).
        Les candidats 'entreprise' stricts sont exclus.
        Crée un enregistrement PetitJobInterest par candidat notifié.
        """
        from app.services.queue_service import send_or_queue
        from app.models.candidate_profile import CandidateProfile
        from app.models.petit_job_interest import PetitJobInterest
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # Charge candidats + leurs profils en une seule requête
        stmt = (
            select(User, CandidateProfile)
            .outerjoin(CandidateProfile, User.id == CandidateProfile.user_id)
            .where(
                User.status == "active",
                User.onboarding_step == "done",
                User.id != job.employeur_user_id,
            )
        )
        rows = (await db.execute(stmt)).all()

        # Filtre usage emploi + calcule le score
        scored: list[tuple[float, User]] = []
        for user, profile in rows:
            usage = user.usage or []
            if isinstance(usage, str):
                usage = [usage]
            if "emploi" not in usage and "tout" not in usage:
                continue

            emploi_type = getattr(profile, "emploi_type", None) if profile else None
            s = self._score_candidate(job, user, emploi_type)
            if s > 0:
                scored.append((s, user))

        # Tri par score décroissant
        scored.sort(key=lambda x: x[0], reverse=True)

        nb_sent = 0
        for s, candidate in scored[:200]:
            try:
                msg = self._build_notification(job, candidate)
                await send_or_queue(db, candidate, msg)
                # Crée l'enregistrement d'intérêt (ON CONFLICT DO NOTHING — idempotent)
                await db.execute(
                    pg_insert(PetitJobInterest).values(
                        id=uuid.uuid4(),
                        petit_job_id=job.id,
                        candidat_user_id=candidate.id,
                        statut="notified",
                    ).on_conflict_do_nothing(constraint="uq_pji_job_candidat")
                )
                nb_sent += 1
            except Exception as e:
                print(f"  [petit_job] notify {candidate.phone_number}: {e}")

        job.nb_candidats_notifies = nb_sent
        await db.flush()
        print(f"  [petit_job] {nb_sent} candidats notifiés pour '{job.titre}'")
        return nb_sent

    async def mark_interested(self, db: AsyncSession, candidat: User) -> "PetitJob | None":
        """
        Marque le candidat comme intéressé pour le dernier job ouvert où il a été notifié.
        Retourne le PetitJob concerné, ou None si aucun trouvé.
        """
        from app.models.petit_job_interest import PetitJobInterest
        from datetime import timezone

        # Dernier intérêt notified/interested de ce candidat (le plus récent)
        stmt = (
            select(PetitJobInterest, PetitJob)
            .join(PetitJob, PetitJobInterest.petit_job_id == PetitJob.id)
            .where(
                PetitJobInterest.candidat_user_id == candidat.id,
                PetitJobInterest.statut.in_(["notified", "interested"]),
                PetitJob.statut == "ouvert",
            )
            .order_by(PetitJobInterest.created_at.desc())
            .limit(1)
        )
        row = (await db.execute(stmt)).first()
        if not row:
            return None

        interest, job = row
        interest.statut = "interested"
        interest.interested_at = datetime.now(timezone.utc)
        await db.flush()
        return job

    async def unlock_contact(
        self,
        db: AsyncSession,
        interest_id: uuid.UUID,
        paydunya_token: str | None = None,
    ) -> tuple["PetitJobInterest | None", User | None, "PetitJob | None"]:
        """
        Débloque le contact d'un candidat après paiement.
        Retourne (interest, candidat, job) ou (None, None, None).
        """
        from app.models.petit_job_interest import PetitJobInterest
        from datetime import timezone

        result = await db.execute(
            select(PetitJobInterest).where(PetitJobInterest.id == interest_id)
        )
        interest = result.scalar_one_or_none()
        if not interest:
            return None, None, None

        # Charger candidat et job
        candidat = (await db.execute(select(User).where(User.id == interest.candidat_user_id))).scalar_one_or_none()
        job = (await db.execute(select(PetitJob).where(PetitJob.id == interest.petit_job_id))).scalar_one_or_none()

        interest.statut = "unlocked"
        interest.unlocked_at = datetime.now(timezone.utc)
        if paydunya_token:
            interest.paydunya_token = paydunya_token
        await db.flush()
        return interest, candidat, job

    async def unlock_all_for_offreur(
        self,
        db: AsyncSession,
        offreur: User,
        paydunya_token: str | None = None,
    ) -> list[tuple[User, "PetitJob"]]:
        """
        Débloque tous les contacts intéressés pour tous les jobs de cet offreur.
        Utilisé lors de l'activation de l'abonnement mensuel.
        """
        from app.models.petit_job_interest import PetitJobInterest
        from datetime import timezone

        now = datetime.now(timezone.utc)

        stmt = (
            select(PetitJobInterest, User, PetitJob)
            .join(PetitJob, PetitJobInterest.petit_job_id == PetitJob.id)
            .join(User, PetitJobInterest.candidat_user_id == User.id)
            .where(
                PetitJob.employeur_user_id == offreur.id,
                PetitJobInterest.statut == "interested",
            )
        )
        rows = (await db.execute(stmt)).all()

        unlocked = []
        for interest, candidat, job in rows:
            interest.statut = "unlocked"
            interest.unlocked_at = now
            if paydunya_token:
                interest.paydunya_token = paydunya_token
            unlocked.append((candidat, job))

        await db.flush()
        return unlocked

    def _build_notification(self, job: PetitJob, user: User) -> str:
        lines = [f"🔔 *Nouveau petit job, {user.name or 'toi'} !*\n"]
        lines.append(f"*{job.titre}*")
        if job.type_travail:
            lines.append(f"📋 Type : {job.type_travail.title()}")
        if job.lieu:
            lines.append(f"📍 Lieu : {job.lieu}")
        if job.date_debut_str:
            lines.append(f"📅 Quand : {job.date_debut_str}")
        if job.duree:
            lines.append(f"⏱ Durée : {job.duree}")
        if job.remuneration:
            lines.append(f"💰 Paie : {job.remuneration}")
        if job.nb_postes and job.nb_postes > 1:
            lines.append(f"👥 Postes : {job.nb_postes}")
        lines.append("\n_Intéressé(e) ? Réponds *je suis intéressé* pour être mis en contact avec l'employeur._")
        return "\n".join(lines)

    async def mark_expired(self, db: AsyncSession) -> int:
        """Passe en 'expiré' les jobs dont la date d'expiration est passée."""
        from sqlalchemy import update
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(PetitJob).where(
                PetitJob.statut == "ouvert",
                PetitJob.expires_at <= now,
            )
        )
        jobs = result.scalars().all()
        for job in jobs:
            job.statut = "expiré"
        await db.flush()
        return len(jobs)


petit_job_service = PetitJobService()
