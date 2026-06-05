"""
Service de matching candidat ↔ offre d'emploi.

Algorithme :
  1. Similarité cosinus embeddings (base 0-100)
  2. Boosts contextuels : localisation, niveau_etudes, type_contrat
  3. Seuil de notification : score >= 65
  4. Top 3 matchs → upsert JobMatch en DB
  5. Génération de conseils lettre de motivation via LLM
"""
import re
import json
import httpx
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.core.settings import get_settings
from app.services.rag.embedding_service import embedding_service

settings = get_settings()

SCORE_SEUIL = 65          # score minimum pour notifier
BOOST_LOCALISATION = 10   # localisation identique ou télétravail
BOOST_NIVEAU = 10         # niveau_etudes compatible
BOOST_CONTRAT = 5         # type_contrat correspond


class MatchingService:

    # ─────────────────────────────────────────────────────────────
    # 1. Score candidat / offre
    # ─────────────────────────────────────────────────────────────

    def compute_score(
        self,
        candidate_embedding: list[float],
        job_embedding: list[float],
        candidate,       # CandidateProfile
        job,             # JobOpportunity
    ) -> float:
        """
        Score 0-100 = similarité cosinus * 100 + boosts contextuels.
        Plafonné à 100.
        """
        base = embedding_service.cosine_similarity(candidate_embedding, job_embedding) * 100

        boost = 0.0

        # Boost localisation
        cand_loc = (candidate.localisation or "").lower().strip()
        job_loc = (job.localisation or "").lower().strip()
        if cand_loc and job_loc:
            if cand_loc == job_loc or "télétravail" in job_loc or "remote" in job_loc:
                boost += BOOST_LOCALISATION

        # Boost niveau_etudes
        niveau_compat = {
            "bac": ["bac"],
            "bac+2": ["bac", "bac+2"],
            "bac+3": ["bac", "bac+2", "bac+3"],
            "bac+5": ["bac", "bac+2", "bac+3", "bac+5"],
            "doctorat": ["bac", "bac+2", "bac+3", "bac+5", "doctorat"],
        }
        cand_niv = (candidate.niveau_etudes or "").lower().strip()
        job_niv = (job.niveau_etudes or "").lower().strip()
        if cand_niv and job_niv:
            acceptes = niveau_compat.get(cand_niv, [cand_niv])
            if job_niv in acceptes or cand_niv == job_niv:
                boost += BOOST_NIVEAU

        # Boost type_contrat — depuis le champ dédié sur CandidateProfile ou via user_contrat
        contrat_souhaite = getattr(candidate, 'type_contrat_souhaite', None)
        job_contrat = (job.type_contrat or "").lower().strip()
        if contrat_souhaite and contrat_souhaite.lower() != "indifferent" and job_contrat:
            if contrat_souhaite.lower() == job_contrat:
                boost += BOOST_CONTRAT

        return min(100.0, round(base + boost, 1))

    # ─────────────────────────────────────────────────────────────
    # 2. Matching complet pour un candidat
    # ─────────────────────────────────────────────────────────────

    async def match_candidate(self, db: AsyncSession, user_id) -> list[dict]:
        """
        Calcule les matchs pour un candidat, upsert les top 3 en DB,
        retourne la liste [{job, score, conseils_lettre}].
        """
        from app.models.candidate_profile import CandidateProfile, JobMatch
        from app.models.job_opportunity import JobOpportunity
        from app.models.user import User

        # Récupère le profil candidat + infos User pour le boost contrat
        result = await db.execute(
            select(CandidateProfile, User)
            .join(User, User.id == CandidateProfile.user_id)
            .where(CandidateProfile.user_id == user_id)
        )
        row = result.first()
        if not row:
            print(f"  → matching: pas de profil pour {user_id}")
            return []
        candidate, user_obj = row

        # Injecte type_contrat_souhaite depuis User sur l'objet candidate pour compute_score
        if not hasattr(candidate, 'type_contrat_souhaite') or candidate.type_contrat_souhaite is None:
            candidate.type_contrat_souhaite = user_obj.type_contrat_souhaite

        if not candidate.embedding:
            print(f"  → matching: pas de profil ou embedding manquant pour {user_id}")
            return []

        # Récupère toutes les offres actives avec embedding
        result = await db.execute(
            select(JobOpportunity).where(
                and_(
                    JobOpportunity.statut == "active",
                    JobOpportunity.embedding.isnot(None),
                )
            )
        )
        jobs = result.scalars().all()

        if not jobs:
            print(f"  → matching: aucune offre active avec embedding")
            return []

        # Calcule les scores
        scored = []
        for job in jobs:
            if not job.embedding:
                continue
            score = self.compute_score(
                candidate_embedding=candidate.embedding,
                job_embedding=job.embedding,
                candidate=candidate,
                job=job,
            )
            if score >= SCORE_SEUIL:
                scored.append((job, score))

        # Tri décroissant par score
        scored.sort(key=lambda x: x[1], reverse=True)
        top3 = scored[:3]

        if not top3:
            print(f"  → matching: aucun match >= {SCORE_SEUIL} pour {user_id}")
            return []

        now = datetime.now(timezone.utc)
        matches_out = []

        for job, score in top3:
            # Upsert JobMatch
            result = await db.execute(
                select(JobMatch).where(
                    and_(JobMatch.user_id == user_id, JobMatch.job_id == job.id)
                )
            )
            existing_match = result.scalar_one_or_none()

            if existing_match:
                existing_match.score_match = score
                existing_match.notifie_at = now
                existing_match.statut = "notifie"
            else:
                new_match = JobMatch(
                    user_id=user_id,
                    job_id=job.id,
                    score_match=score,
                    notifie_at=now,
                    statut="notifie",
                )
                db.add(new_match)

            # Génère conseils lettre de motivation
            conseils = await self._generate_conseils(candidate, job)

            matches_out.append({
                "job": {
                    "id": str(job.id),
                    "titre": job.titre,
                    "entreprise": job.entreprise,
                    "secteur": job.secteur,
                    "localisation": job.localisation,
                    "type_contrat": job.type_contrat,
                    "niveau_etudes": job.niveau_etudes,
                },
                "score": score,
                "conseils_lettre": conseils,
            })

        await db.flush()
        print(f"  → matching: {len(matches_out)} match(s) >= {SCORE_SEUIL} pour {user_id}")
        return matches_out

    # ─────────────────────────────────────────────────────────────
    # 3. Conseils lettre de motivation
    # ─────────────────────────────────────────────────────────────

    async def _generate_conseils(self, candidate, job) -> list[str]:
        """
        Génère 4 bullets de conseils pour la lettre de motivation
        basés sur le profil candidat et la description du poste.
        """
        competences = ", ".join((candidate.competences or [])[:8])
        secteurs = ", ".join((candidate.secteurs_interets or [])[:4])
        job_desc = (job.description or "")[:800]

        prompt = f"""Tu aides un candidat africain à rédiger sa lettre de motivation.

Profil candidat :
- Compétences : {competences or 'non précisées'}
- Secteurs d'intérêt : {secteurs or 'non précisés'}
- Niveau d'études : {candidate.niveau_etudes or 'non précisé'}
- Expérience : {candidate.annees_experience} an(s)

Poste visé : {job.titre} chez {job.entreprise}
Description : {job_desc}

Donne exactement 4 conseils courts (1 phrase chacun) pour personnaliser la lettre.
Retourne UNIQUEMENT un JSON :
{{"conseils": ["conseil 1", "conseil 2", "conseil 3", "conseil 4"]}}"""

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 300,
                        "temperature": 0.3,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    return self._default_conseils(job)
                raw = data["choices"][0]["message"]["content"].strip()
                raw = re.sub(r"```json|```", "", raw).strip()
                parsed = json.loads(raw)
                conseils = parsed.get("conseils", [])
                if isinstance(conseils, list) and len(conseils) >= 1:
                    return conseils[:4]
        except Exception as e:
            print(f"  ⚠️ matching conseils LLM: {e}")

        return self._default_conseils(job)

    def _default_conseils(self, job) -> list[str]:
        """Conseils génériques si LLM indisponible."""
        return [
            f"Mentionnez explicitement le poste '{job.titre}' dès l'introduction.",
            f"Mettez en avant vos compétences les plus pertinentes pour {job.entreprise}.",
            "Montrez votre motivation pour le secteur avec un exemple concret.",
            "Concluez avec une disponibilité claire et une invitation à un entretien.",
        ]

    # ─────────────────────────────────────────────────────────────
    # 4. Matching global — déclenché après validation d'une annonce
    # ─────────────────────────────────────────────────────────────

    async def match_all_candidates(self, db: AsyncSession, job) -> int:
        """
        Calcule le matching entre une nouvelle annonce et TOUS les candidats
        qui ont un profil + embedding.

        Pour chaque candidat avec score >= 65 :
          - Crée/update JobMatch en DB
          - Envoie notification WhatsApp avec le détail + conseils lettre

        Retourne le nombre de candidats notifiés.
        """
        from app.models.candidate_profile import CandidateProfile, JobMatch
        from app.models.user import User
        from app.services.whatsapp.sender import whatsapp_sender

        if not job.embedding:
            print(f"  ⚠️ match_all_candidates: annonce {job.id} sans embedding — skip")
            return 0

        result = await db.execute(
            select(CandidateProfile, User)
            .join(User, User.id == CandidateProfile.user_id)
            .where(CandidateProfile.embedding.isnot(None))
        )
        rows = result.all()

        if not rows:
            print(f"  → match_all_candidates: aucun profil candidat avec embedding")
            return 0

        now = datetime.now(timezone.utc)
        notified = 0

        for candidate, user_obj in rows:
            # Injecte type_contrat_souhaite depuis User
            candidate.type_contrat_souhaite = getattr(candidate, 'type_contrat_souhaite', None) or user_obj.type_contrat_souhaite
            score = self.compute_score(
                candidate_embedding=candidate.embedding,
                job_embedding=job.embedding,
                candidate=candidate,
                job=job,
            )
            if score < SCORE_SEUIL:
                continue

            # Upsert JobMatch
            match_res = await db.execute(
                select(JobMatch).where(
                    and_(JobMatch.user_id == candidate.user_id, JobMatch.job_id == job.id)
                )
            )
            existing = match_res.scalar_one_or_none()
            if existing:
                existing.score_match = score
                existing.notifie_at = now
                existing.statut = "notifie"
            else:
                db.add(JobMatch(
                    user_id=candidate.user_id,
                    job_id=job.id,
                    score_match=score,
                    notifie_at=now,
                    statut="notifie",
                ))

            # Utilise l'objet user_obj déjà chargé en join
            user = user_obj
            if not user or not user.phone_number:
                continue

            # Génère conseils lettre
            conseils = await self._generate_conseils(candidate, job)

            # Notification WhatsApp
            msg = (
                f"✅ *Nouvelle opportunité pour toi !*\n\n"
                f"*{job.titre}*\n"
                f"🏢 {job.entreprise}"
            )
            if job.localisation:
                msg += f" • 📍 {job.localisation}"
            msg += f"\n🎯 Compatibilité : *{int(score)}%*\n\n"
            if job.type_contrat:
                msg += f"📋 Contrat : {job.type_contrat}\n"
            if job.niveau_etudes:
                msg += f"🎓 Niveau : {job.niveau_etudes}\n"
            if job.email_candidature:
                msg += f"\n📧 Postuler : {job.email_candidature}\n"
            if conseils:
                msg += "\n*💡 Conseils pour ta lettre :*\n"
                for c in conseils[:4]:
                    msg += f"• {c}\n"

            try:
                await whatsapp_sender.send_text(user.phone_number, msg)
                notified += 1
            except Exception as e:
                print(f"  ⚠️ match_all_candidates WhatsApp {user.phone_number}: {e}")

        try:
            await db.flush()
        except Exception as e:
            print(f"  ⚠️ match_all_candidates flush: {e}")

        print(f"  → match_all_candidates: {notified} candidats notifiés pour annonce {job.id}")
        return notified


matching_service = MatchingService()
