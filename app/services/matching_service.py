"""
Service de matching candidat ↔ offre d'emploi — architecture 3 couches.

  Couche 1 — filtre cosinus rapide (embeddings)        → écarte les non-pertinents
  Couche 2 — LLM juge la compatibilité métier (0-100)  → décision fine
  Couche 3 — vérification des conditions (informatif)  → niveau/expérience

Score final = cosinus*40% + LLM*60%.
Quota hebdo : gratuit = 1 notif/semaine, pro = illimité.
"""
import re
import json
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.core.settings import get_settings
from app.services.rag.embedding_service import embedding_service

settings = get_settings()

COSINE_MIN = 0.35    # seuil couche 1
LLM_MIN = 50         # seuil couche 2
NIVEAUX = ["bac", "bac+2", "bac+3", "bac+5", "doctorat"]


class MatchingService:

    # ═══════════════════════════════════════════════════════════════
    # Entrée principale — un candidat contre toutes les offres
    # ═══════════════════════════════════════════════════════════════

    async def match_candidate(self, db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
        """Matching 3 couches d'un candidat contre toutes les offres actives."""
        candidate, user = await self._load_candidate_and_user(db, user_id)
        if not candidate or not candidate.embedding:
            print(f"  → matching: pas de profil/embedding pour {user_id}")
            return []

        if not self._check_quota(candidate, user):
            print(f"  → Quota notifs atteint pour {user_id}")
            # Upsell : l'utilisateur gratuit a déjà reçu son offre de la semaine
            if getattr(user, "plan", "free") != "pro" and user.phone_number:
                try:
                    from app.services.whatsapp.messages import messages
                    from app.services.payment_service import payment_service
                    from app.services.whatsapp.sender import whatsapp_sender
                    payment_url = None
                    try:
                        inv = await payment_service.create_invoice(user=user, plan="pro")
                        if inv.get("success") and inv.get("payment_url"):
                            payment_url = inv["payment_url"]
                    except Exception:
                        pass
                    await whatsapp_sender.send_text(
                        user.phone_number,
                        "💼 Tu as déjà reçu ton offre gratuite de la semaine !\n\n"
                        + messages.pro_upsell(user.name or "toi", "emploi", payment_url)
                    )
                except Exception as e:
                    print(f"  ⚠️ upsell emploi error: {e}")
            return []

        jobs = await self._load_active_jobs(db)
        if not jobs:
            return []

        results = []
        for job in jobs:
            if not job.embedding:
                continue
            # Couche 1 — cosinus
            cosine = embedding_service.cosine_similarity(candidate.embedding, job.embedding)
            if cosine < COSINE_MIN:
                continue
            # Couche 2 — LLM
            llm_result = await self._llm_match(candidate, job)
            if not llm_result or llm_result.get("score", 0) < LLM_MIN:
                continue
            # Couche 3 — conditions (informatif)
            conditions_check = self._check_conditions(candidate, job)
            final_score = (cosine * 100 * 0.4) + (llm_result["score"] * 0.6)
            results.append({
                "job": job,
                "score": round(final_score),
                "raison": llm_result.get("raison", ""),
                "conditions_check": conditions_check,
                "conseils_lettre": llm_result.get("conseils", []),
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        plan = getattr(user, "plan", "free")
        if plan == "pro":
            top = results
        else:
            max_to_send = 1 + (getattr(user, "extra_job_offers_bonus", 0) or 0)
            top = results[:max_to_send]

        for match in top:
            await self._notify_candidate(user, match)
            await self._upsert_jobmatch(db, user_id, match["job"].id, match["score"])

        if top:
            await self._update_quota(db, candidate, len(top))

        # Upsell post-offre pour les utilisateurs gratuits
        if top and plan != "pro" and getattr(user, "phone_number", None):
            try:
                from app.services.whatsapp.messages import messages
                from app.services.payment_service import payment_service
                from app.services.whatsapp.sender import whatsapp_sender
                payment_url = None
                try:
                    inv = await payment_service.create_invoice(user=user, plan="pro")
                    if inv.get("success") and inv.get("payment_url"):
                        payment_url = inv["payment_url"]
                except Exception:
                    pass
                upsell_msg = (
                    "💼 *Tu viens de recevoir ton offre gratuite de la semaine !*\n\n"
                    "Passe en *Pro* pour recevoir toutes les offres qui correspondent à ton profil, "
                    "dès qu'elles sont disponibles — sans attendre.\n\n"
                    + messages.pro_upsell(user.name or "toi", "emploi", payment_url)
                )
                await whatsapp_sender.send_text(user.phone_number, upsell_msg)
            except Exception as e:
                print(f"  ⚠️ upsell post-offre error: {e}")

        return top

    # ═══════════════════════════════════════════════════════════════
    # Entrée secondaire — une offre contre tous les candidats
    # (déclenché après validation d'une annonce par l'admin)
    # ═══════════════════════════════════════════════════════════════

    async def match_all_candidates(self, db: AsyncSession, job) -> int:
        """Matche une nouvelle annonce contre tous les candidats éligibles."""
        from app.models.candidate_profile import CandidateProfile
        from app.models.user import User

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
            return 0

        notified = 0
        for candidate, user in rows:
            if not self._check_quota(candidate, user):
                continue
            cosine = embedding_service.cosine_similarity(candidate.embedding, job.embedding)
            if cosine < COSINE_MIN:
                continue
            llm_result = await self._llm_match(candidate, job)
            if not llm_result or llm_result.get("score", 0) < LLM_MIN:
                continue
            conditions_check = self._check_conditions(candidate, job)
            final_score = round((cosine * 100 * 0.4) + (llm_result["score"] * 0.6))
            match = {
                "job": job,
                "score": final_score,
                "raison": llm_result.get("raison", ""),
                "conditions_check": conditions_check,
                "conseils_lettre": llm_result.get("conseils", []),
            }
            await self._notify_candidate(user, match)
            await self._upsert_jobmatch(db, user.id, job.id, final_score)
            await self._update_quota(db, candidate, 1)
            notified += 1

        print(f"  → match_all_candidates: {notified} candidats notifiés pour {job.id}")
        return notified

    # ═══════════════════════════════════════════════════════════════
    # Chargement
    # ═══════════════════════════════════════════════════════════════

    async def _load_candidate_and_user(self, db: AsyncSession, user_id: uuid.UUID):
        from app.models.candidate_profile import CandidateProfile
        from app.models.user import User
        result = await db.execute(
            select(CandidateProfile, User)
            .join(User, User.id == CandidateProfile.user_id)
            .where(CandidateProfile.user_id == user_id)
        )
        row = result.first()
        if not row:
            return None, None
        return row[0], row[1]

    async def _load_active_jobs(self, db: AsyncSession) -> list:
        from app.models.job_opportunity import JobOpportunity
        result = await db.execute(
            select(JobOpportunity).where(
                and_(
                    JobOpportunity.statut == "active",
                    JobOpportunity.embedding.isnot(None),
                )
            )
        )
        return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════════
    # Couche 2 — LLM
    # ═══════════════════════════════════════════════════════════════

    async def _llm_match(self, candidate, job) -> dict:
        """LLM juge la compatibilité entre candidat et offre."""
        if not settings.mistral_api_key:
            return {}

        # Nettoyage défensif : couvre les offres déjà en DB dont le titre/desc
        # contient des caractères de contrôle/guillemets cassant le JSON.
        from app.services.scraping_service import clean_text

        def _cl(s):
            return clean_text(s) or ""

        resume = _cl(candidate.resume_profil)
        metiers = _cl(", ".join((candidate.metiers_cibles or [])[:5]))
        competences = _cl(", ".join((candidate.competences_normalisees or [])[:8]))

        job_desc = f"{_cl(job.titre)} - {_cl(job.entreprise)}\n"
        job_desc += f"Secteur: {_cl(job.secteur)}\n"
        job_desc += f"Description: {_cl((job.description_complete or job.description or ''))[:500]}\n"
        if job.taches:
            job_desc += f"Missions: {_cl(', '.join(str(t) for t in job.taches[:3]))}\n"
        if job.conditions_requises:
            job_desc += f"Conditions: {_cl(', '.join(str(c) for c in job.conditions_requises[:3]))}\n"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content":
                            f"Évalue la compatibilité entre ce candidat et cette offre d'emploi au Sénégal.\n\n"
                            f"CANDIDAT:\n"
                            f"Profil: {resume}\n"
                            f"Métiers cibles: {metiers}\n"
                            f"Compétences: {competences}\n"
                            f"Niveau: {candidate.niveau_etudes or 'non précisé'}\n"
                            f"Expérience: {candidate.annees_experience or 0} ans\n\n"
                            f"OFFRE:\n{job_desc}\n\n"
                            f"Réponds UNIQUEMENT avec ce JSON:\n"
                            f'{{"score": 0-100, '
                            f'"compatible": true|false, '
                            f'"raison": "explication courte pourquoi compatible ou non", '
                            f'"conseils": ["conseil lettre 1", "conseil lettre 2", "conseil lettre 3"]}}'
                        }],
                        "temperature": 0.1,
                        "max_tokens": 200,
                    },
                )
                data = resp.json()
                if "choices" in data:
                    txt = data["choices"][0]["message"]["content"].strip()
                    txt = re.sub(r"```json|```", "", txt).strip()

                    # Tente parsing direct
                    try:
                        parsed = json.loads(txt)
                    except json.JSONDecodeError:
                        # Fallback — extrait les champs avec regex
                        score_match = re.search(r'"score"\s*:\s*(\d+)', txt)
                        if not score_match:
                            print(f"  ⚠️ LLM match JSON illisible: {txt[:120]}")
                            return {}
                        compatible_match = re.search(r'"compatible"\s*:\s*(true|false)', txt)
                        raison_match = re.search(r'"raison"\s*:\s*"([^"]*)"', txt)
                        parsed = {
                            "score": int(score_match.group(1)),
                            "compatible": compatible_match.group(1) == "true" if compatible_match else True,
                            "raison": raison_match.group(1) if raison_match else "",
                            "conseils": [],
                        }

                    try:
                        parsed["score"] = int(parsed.get("score", 0))
                    except (TypeError, ValueError):
                        parsed["score"] = 0
                    return parsed
        except Exception as e:
            print(f"  ⚠️ LLM match error: {e}")
        return {}

    # ═══════════════════════════════════════════════════════════════
    # Couche 3 — conditions (informatif)
    # ═══════════════════════════════════════════════════════════════

    def _check_conditions(self, candidate, job) -> list[dict]:
        checks = []
        # Niveau études
        if job.niveau_etudes and candidate.niveau_etudes:
            req_idx = next((i for i, n in enumerate(NIVEAUX) if n in (job.niveau_etudes or "").lower()), -1)
            cand_idx = next((i for i, n in enumerate(NIVEAUX) if n in (candidate.niveau_etudes or "").lower()), -1)
            if req_idx >= 0 and cand_idx >= 0:
                checks.append({
                    "label": f"Niveau requis: {job.niveau_etudes}",
                    "ok": cand_idx >= req_idx,
                    "candidat": candidate.niveau_etudes,
                })
        # Expérience
        if job.experience_min_annees and job.experience_min_annees > 0:
            exp_cand = candidate.annees_experience or 0
            checks.append({
                "label": f"{job.experience_min_annees} ans d'expérience requis",
                "ok": exp_cand >= job.experience_min_annees,
                "candidat": f"{exp_cand} ans",
            })
        return checks

    # ═══════════════════════════════════════════════════════════════
    # Quota
    # ═══════════════════════════════════════════════════════════════

    def _check_quota(self, candidate, user) -> bool:
        plan = getattr(user, "plan", "free")
        if plan == "pro":
            return True
        max_notifs = 1 + (getattr(user, "extra_job_offers_bonus", 0) or 0)
        now = datetime.now(timezone.utc)
        last_notif = candidate.last_notif_at
        if last_notif:
            week_ago = now - timedelta(days=7)
            if last_notif > week_ago and (candidate.nb_notifs_semaine or 0) >= max_notifs:
                return False
        return True

    async def _update_quota(self, db: AsyncSession, candidate, nb_sent: int):
        now = datetime.now(timezone.utc)
        # Réinitialise le compteur si la dernière notif date de plus d'une semaine
        if candidate.last_notif_at and candidate.last_notif_at < (now - timedelta(days=7)):
            candidate.nb_notifs_semaine = 0
        candidate.nb_notifs_semaine = (candidate.nb_notifs_semaine or 0) + nb_sent
        candidate.last_notif_at = now
        await db.flush()

    # ═══════════════════════════════════════════════════════════════
    # Persistance JobMatch
    # ═══════════════════════════════════════════════════════════════

    async def _upsert_jobmatch(self, db: AsyncSession, user_id, job_id, score: float):
        from app.models.candidate_profile import JobMatch
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(JobMatch).where(
                and_(JobMatch.user_id == user_id, JobMatch.job_id == job_id)
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.score_match = score
            existing.notifie_at = now
            existing.statut = "notifie"
        else:
            db.add(JobMatch(
                user_id=user_id, job_id=job_id,
                score_match=score, notifie_at=now, statut="notifie",
            ))
        await db.flush()

    # ═══════════════════════════════════════════════════════════════
    # Notification WhatsApp
    # ═══════════════════════════════════════════════════════════════

    async def _notify_candidate(self, user, match: dict):
        job = match["job"]
        score = match["score"]
        raison = match.get("raison", "")
        conditions = match.get("conditions_check", [])
        conseils = match.get("conseils_lettre", [])

        if not getattr(user, "phone_number", None):
            return

        msg = f"✅ *Opportunité compatible à {score}%*\n\n"
        msg += f"📌 *{job.titre}* — {job.entreprise}\n"
        if job.localisation:
            msg += f"📍 {job.localisation}"
        if job.type_contrat:
            msg += f" · {job.type_contrat}"
        msg += "\n\n"

        if job.taches:
            msg += "*📋 Missions :*\n"
            for t in job.taches[:3]:
                msg += f"• {t}\n"
            msg += "\n"

        if raison:
            msg += f"*💡 Pourquoi tu corresponds :*\n{raison}\n\n"

        if conditions:
            msg += "*✅ Conditions :*\n"
            for c in conditions:
                icon = "✅" if c["ok"] else "⚠️"
                msg += f"{icon} {c['label']} → tu as {c['candidat']}\n"
            msg += "\n"

        if job.email_candidature:
            msg += f"*📧 Pour postuler :* {job.email_candidature}\n"
        elif job.source_url:
            msg += f"*🔗 Voir l'offre :* {job.source_url}\n"
        msg += "\n"

        if conseils:
            msg += "*💡 Conseils pour ta lettre :*\n"
            for c in conseils[:3]:
                msg += f"• {c}\n"

        from app.services.whatsapp.sender import whatsapp_sender
        try:
            await whatsapp_sender.send_text(user.phone_number, msg)
            print(f"  → Notif emploi → {user.phone_number}: {job.titre} ({score}%)")
        except Exception as e:
            print(f"  ⚠️ Notif WhatsApp échouée {user.phone_number}: {e}")


matching_service = MatchingService()


async def run_match_all_candidates_bg(job_id: uuid.UUID | str):
    """
    Wrapper pour exécution en arrière-plan (asyncio.create_task).
    Ouvre sa PROPRE session DB — ne réutilise pas celle de la requête HTTP
    (qui serait fermée avant la fin du matching).
    """
    from app.db.database import AsyncSessionLocal
    from app.models.job_opportunity import JobOpportunity

    try:
        job_uuid = uuid.UUID(str(job_id))
    except (ValueError, TypeError):
        print(f"  ⚠️ run_match_all_candidates_bg: job_id invalide {job_id}")
        return

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(JobOpportunity).where(JobOpportunity.id == job_uuid)
            )
            job = result.scalar_one_or_none()
            if not job:
                print(f"  ⚠️ run_match_all_candidates_bg: annonce {job_id} introuvable")
                return
            await matching_service.match_all_candidates(db, job)
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"  ⚠️ run_match_all_candidates_bg error: {e}")
