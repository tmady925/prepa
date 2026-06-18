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

    async def notify_candidates(self, db: AsyncSession, job: PetitJob) -> int:
        """Notifie les candidats emploi dont la localisation correspond."""
        from app.services.whatsapp.sender import whatsapp_sender
        from app.services.queue_service import send_or_queue

        stmt = select(User).where(
            User.status == "active",
            User.onboarding_step == "done",
            User.id != job.employeur_user_id,
        )
        result = await db.execute(stmt)
        candidates = result.scalars().all()

        # Filtre par localisation si disponible
        if job.lieu:
            job_lieu = job.lieu.lower()
            matched = [
                u for u in candidates
                if getattr(u, "localisation_emploi", None)
                and (job_lieu in u.localisation_emploi.lower()
                     or u.localisation_emploi.lower() in job_lieu)
            ]
            # Si aucun match géo → notifie tous (première itération)
            targets = matched if matched else candidates
        else:
            targets = candidates

        # Filtre sur usage emploi
        def _has_emploi(u) -> bool:
            usage = u.usage or []
            if isinstance(usage, str):
                usage = [usage]
            return "emploi" in usage or "tout" in usage

        targets = [u for u in targets if _has_emploi(u)]

        nb_sent = 0
        for candidate in targets[:200]:  # cap sécurité
            try:
                msg = self._build_notification(job, candidate)
                await send_or_queue(db, candidate, msg)
                nb_sent += 1
            except Exception as e:
                print(f"  [petit_job] notify {candidate.phone_number}: {e}")

        # Met à jour le compteur
        job.nb_candidats_notifies = nb_sent
        await db.flush()

        return nb_sent

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
