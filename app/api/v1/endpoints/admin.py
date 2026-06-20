import uuid
from datetime import date, datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date

# Matières acceptées en base — normalise les variantes courantes
_MATIERE_ALIASES = {
    "chimie": "physique_chimie",
    "physique": "physique_chimie",
    "pc": "physique_chimie",
    "physique chimie": "physique_chimie",
    "physique-chimie": "physique_chimie",
    "svt": "svt",
    "bio": "svt",
    "biologie": "svt",
    "maths": "maths",
    "mathematiques": "maths",
    "mathématiques": "maths",
    "francais": "francais",
    "français": "francais",
    "philo": "philosophie",
    "hist": "histoire_geo",
    "histoire": "histoire_geo",
    "geo": "histoire_geo",
    "anglais": "anglais",
}

_VALID_EXAM_TYPES = {"bac_senegal", "bfem", "concours", "bac_cote_ivoire", "bac_mali", "autre"}
_VALID_MATIERES = {"maths", "physique_chimie", "svt", "francais", "philosophie", "histoire_geo", "anglais", "autre"}
_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

def _validate_annee(val) -> int | None:
    """Retourne l'année en int si valide (4 chiffres entre 1990 et 2100), sinon None."""
    if val is None:
        return None
    try:
        year = int(str(val).strip())
        if 1990 <= year <= 2100:
            return year
    except (ValueError, TypeError):
        pass
    return None

def _sanitize_chapitre(c: str | None) -> str | None:
    """Supprime les caractères dangereux pour les chemins de fichiers."""
    if not c:
        return c
    import re as _re
    return _re.sub(r'[^\w\-]', '_', c.strip().lower())[:80]

def _normalize_matiere(m: str | None) -> str | None:
    if not m:
        return m
    return _MATIERE_ALIASES.get(m.strip().lower(), m.strip().lower())
sa_select = select
from app.db.database import get_db
from app.core.settings import get_settings
from app.models.user import User
from app.models.subscription import Subscription
from app.models.message import Message
from app.services.config_service import config_service, DEFAULTS

# Coût estimé par 1 000 tokens (USD)
PROVIDER_COST_PER_1K = {
    "openai":    0.00015,   # gpt-4o-mini
    "anthropic": 0.00025,   # claude-haiku
    "mistral":   0.00020,   # mistral-small
    "groq":      0.00000,   # gratuit
}

settings = get_settings()
router = APIRouter()


def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != settings.admin_secret_key:
        raise HTTPException(status_code=401, detail="Non autorisé")
    return True


# ── API JSON ──────────────────────────────────────────────────────────

@router.get("/admin/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(select(func.count(User.id)).where(User.status == "active"))
    pro_users = await db.scalar(select(func.count(User.id)).where(User.plan == "pro"))
    total_messages = await db.scalar(select(func.sum(User.total_messages)))
    total_revenue = await db.scalar(select(func.sum(Subscription.amount_fcfa)).where(Subscription.status == "active"))

    today = date.today()

    # Tokens aujourd'hui (messages sortants avec LLM)
    tokens_today = await db.scalar(
        select(func.sum(Message.tokens_used)).where(
            Message.direction == "outbound",
            cast(Message.created_at, Date) == today,
        )
    ) or 0

    # Tokens total (toutes périodes)
    tokens_total = await db.scalar(
        select(func.sum(Message.tokens_used)).where(Message.direction == "outbound")
    ) or 0

    # Dépenses par provider aujourd'hui
    provider_rows = await db.execute(
        select(Message.llm_provider, func.sum(Message.tokens_used)).where(
            Message.direction == "outbound",
            cast(Message.created_at, Date) == today,
            Message.llm_provider.isnot(None),
        ).group_by(Message.llm_provider)
    )
    cost_today_usd = 0.0
    provider_breakdown = {}
    for provider, tokens in provider_rows:
        rate = PROVIDER_COST_PER_1K.get(provider, 0.0)
        cost = round((tokens or 0) / 1000 * rate, 6)
        cost_today_usd += cost
        provider_breakdown[provider] = {"tokens": tokens or 0, "cost_usd": cost}

    # Dépenses totales par provider (toutes périodes)
    provider_rows_total = await db.execute(
        select(Message.llm_provider, func.sum(Message.tokens_used)).where(
            Message.direction == "outbound",
            Message.llm_provider.isnot(None),
        ).group_by(Message.llm_provider)
    )
    cost_total_usd = 0.0
    for provider, tokens in provider_rows_total:
        rate = PROVIDER_COST_PER_1K.get(provider, 0.0)
        cost_total_usd += round((tokens or 0) / 1000 * rate, 6)

    return {
        "total_users": total_users or 0,
        "active_users": active_users or 0,
        "pro_users": pro_users or 0,
        "free_users": (active_users or 0) - (pro_users or 0),
        "total_messages": total_messages or 0,
        "total_revenue_fcfa": total_revenue or 0,
        "conversion_rate": round((pro_users or 0) / max(active_users or 1, 1) * 100, 1),
        "tokens_today": tokens_today,
        "tokens_total": tokens_total,
        "cost_today_usd": round(cost_today_usd, 4),
        "cost_total_usd": round(cost_total_usd, 4),
        "provider_breakdown": provider_breakdown,
    }


@router.get("/admin/users")
async def get_users(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    limit: int = 50,
    offset: int = 0,
):
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    )
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "phone": u.phone_number,
            "name": u.name,
            "plan": u.plan,
            "status": u.status,
            "exam_type": u.exam_type,
            "series": u.series,
            "streak_days": u.streak_days,
            "total_messages": u.total_messages,
            "engagement_score": u.engagement_score,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.get("/admin/config")
async def get_config(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.config import PlatformConfig
    result = await db.execute(select(PlatformConfig).order_by(PlatformConfig.key))
    configs = result.scalars().all()

    config_list = []
    for key, default in DEFAULTS.items():
        db_val = next((c.value for c in configs if c.key == key and c.scope == "global"), None)
        config_list.append({
            "key": key,
            "value": db_val if db_val is not None else default,
            "default": default,
            "is_custom": db_val is not None,
        })
    return config_list


@router.post("/admin/config")
async def update_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    data = await request.json()
    key = data.get("key")
    value = data.get("value")
    if not key or value is None:
        raise HTTPException(status_code=400, detail="key et value requis")
    await config_service.set(key, value, updated_by="admin")
    return {"status": "ok", "key": key, "value": value}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    import uuid
    from sqlalchemy import delete as sa_delete
    from app.models.subscription import Subscription
    from app.models.message import Message

    uid = uuid.UUID(user_id)

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    # Supprime les données liées d'abord
    await db.execute(sa_delete(Subscription).where(Subscription.user_id == uid))
    await db.execute(sa_delete(Message).where(Message.user_id == uid))

    # Supprime l'utilisateur
    await db.delete(user)
    await db.commit()
    return {"status": "ok"}


@router.post("/admin/users/{user_id}/reset-quota")
async def reset_quota(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from sqlalchemy import update
    import uuid
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(user_id))
        .values(daily_messages_used=0, daily_messages_bonus=100)
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/admin/users/{user_id}/activate-pro")
async def activate_pro_admin(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    import uuid
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    from app.services.payment_service import payment_service
    await payment_service.activate_pro(db=db, user=user, paydunya_token="admin_manual")
    await db.commit()
    return {"status": "ok"}


@router.post("/admin/documents/upload")
async def upload_document(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité documents/cours désactivée (plateforme emploi uniquement).")


@router.post("/admin/documents/upload-exercises")
async def upload_exercise_document(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité exercices désactivée (plateforme emploi uniquement).")


@router.post("/admin/exercises/upload")
async def upload_exercise(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité exercices désactivée (plateforme emploi uniquement).")


@router.get("/admin/exercises")
async def get_exercises(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    matiere: str | None = None,
    niveau: int | None = None,
    exam_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    return []


@router.post("/admin/exercises/fix-matieres")
async def fix_exercise_matieres(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité exercices désactivée (plateforme emploi uniquement).")


@router.delete("/admin/exercises/{exercise_id}")
async def delete_exercise(
    exercise_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité exercices désactivée (plateforme emploi uniquement).")


@router.get("/admin/subscriptions")
async def get_subscriptions(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    limit: int = 100,
    offset: int = 0,
):
    """Liste les abonnements avec infos utilisateur et statistiques."""
    # Récupère les abonnements avec jointure utilisateur
    result = await db.execute(
        select(Subscription, User)
        .join(User, Subscription.user_id == User.id)
        .order_by(Subscription.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    # Stats globales
    total_revenue = await db.scalar(
        select(func.sum(Subscription.amount_fcfa)).where(Subscription.status == "active")
    ) or 0
    active_count = await db.scalar(
        select(func.count(Subscription.id)).where(Subscription.status == "active")
    ) or 0
    total_count = await db.scalar(select(func.count(Subscription.id))) or 0

    subscriptions = []
    for sub, user in rows:
        subscriptions.append({
            "id": str(sub.id),
            "user_id": str(sub.user_id),
            "user_name": user.name if user else None,
            "user_phone": user.phone_number if user else None,
            "plan": sub.plan,
            "status": sub.status,
            "amount_fcfa": sub.amount_fcfa,
            "payment_method": sub.payment_method,
            "started_at": sub.started_at.isoformat() if sub.started_at else None,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "paydunya_token": sub.paydunya_token,
            "referral_code_used": sub.referral_code_used,
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
        })

    return {
        "subscriptions": subscriptions,
        "stats": {
            "total_revenue_fcfa": total_revenue,
            "active_count": active_count,
            "total_count": total_count,
            "conversion_rate": round(active_count / max(total_count, 1) * 100, 1),
        },
    }


@router.get("/admin/documents")
async def get_documents(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    return []


@router.delete("/admin/documents/{document_id}")
async def delete_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité documents désactivée.")


# ── NOTIFICATIONS ─────────────────────────────────────────────────────

@router.post("/admin/notifications/count")
async def notifications_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.notification_service import notification_service
    data = await request.json()
    count = await notification_service.count_targets(
        db=db,
        filter_type=data.get("filter_type", "all"),
        exam_type=data.get("exam_type"),
        series=data.get("series"),
    )
    return {"count": count}


@router.post("/admin/notifications/send")
async def notifications_send(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.notification_service import notification_service
    data = await request.json()
    result = await notification_service.send_campaign(
        db=db,
        filter_type=data.get("filter_type", "all"),
        message_type=data.get("message_type", "custom"),
        custom_message=data.get("custom_message"),
        exam_type=data.get("exam_type"),
        series=data.get("series"),
    )
    return result


# ── EXAMENS / SÉRIES / CONCOURS / SIMULATIONS — désactivés ───────────

@router.get("/admin/exams")
async def get_exams(db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    return []

@router.post("/admin/exams")
async def create_exam(request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité examens désactivée (plateforme emploi uniquement).")

@router.put("/admin/exams/{exam_id}")
async def update_exam(exam_id: str, request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité examens désactivée (plateforme emploi uniquement).")

@router.delete("/admin/exams/{exam_id}")
async def delete_exam(exam_id: str, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité examens désactivée (plateforme emploi uniquement).")


@router.get("/admin/series")
async def get_series(exam_id: str = None, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    return []

@router.post("/admin/series")
async def create_series(request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité séries désactivée (plateforme emploi uniquement).")

@router.put("/admin/series/{series_id}")
async def update_series(series_id: str, request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité séries désactivée (plateforme emploi uniquement).")

@router.delete("/admin/series/{series_id}")
async def delete_series(series_id: str, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité séries désactivée (plateforme emploi uniquement).")


@router.get("/admin/concours")
async def get_concours(db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    return []

@router.post("/admin/concours")
async def create_concours(request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité concours désactivée (plateforme emploi uniquement).")

@router.put("/admin/concours/{concours_id}")
async def update_concours(concours_id: str, request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité concours désactivée (plateforme emploi uniquement).")

@router.delete("/admin/concours/{concours_id}")
async def delete_concours(concours_id: str, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité concours désactivée (plateforme emploi uniquement).")


@router.get("/admin/simulations")
async def get_simulations(db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin), statut: str | None = None):
    return []

@router.post("/admin/simulations")
async def create_simulation(request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")

@router.put("/admin/simulations/{simulation_id}")
async def update_simulation(simulation_id: str, request: Request, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")

@router.delete("/admin/simulations/{simulation_id}")
async def delete_simulation(simulation_id: str, db: AsyncSession = Depends(get_db), _: bool = Depends(verify_admin)):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")


@router.post("/admin/simulations/{simulation_id}/notifier")
async def notifier_simulation(
    simulation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")


@router.post("/admin/simulations/{simulation_id}/lancer")
async def lancer_simulation_now(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")


@router.post("/admin/simulations/{simulation_id}/corriger")
async def corriger_simulation_now(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    raise HTTPException(status_code=410, detail="Fonctionnalité simulations désactivée (plateforme emploi uniquement).")


@router.get("/admin/simulations/{simulation_id}/participants")
async def get_simulation_participants(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    return []


# ── RECRUTEURS ────────────────────────────────────────────────────────

@router.get("/admin/recruiters")
async def list_recruiters(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    statut: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Liste tous les recruteurs avec nb annonces chacun."""
    from app.models.recruiter import Recruiter
    from app.models.job_opportunity import JobOpportunity

    q = select(Recruiter).order_by(Recruiter.created_at.desc())
    if statut:
        q = q.where(Recruiter.statut == statut)
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    recruiters = result.scalars().all()

    out = []
    for r in recruiters:
        nb_jobs = await db.scalar(
            select(func.count(JobOpportunity.id)).where(JobOpportunity.recruiter_id == r.id)
        ) or 0
        out.append({
            "id": str(r.id),
            "nom": r.nom,
            "entreprise": r.entreprise,
            "email": r.email,
            "phone": r.phone,
            "plan": r.plan,
            "statut": r.statut,
            "annonces_restantes": r.annonces_restantes,
            "nb_annonces": nb_jobs,
            "abonnement_expire_at": r.abonnement_expire_at.isoformat() if r.abonnement_expire_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


@router.post("/admin/recruiters/{recruiter_id}/activate")
async def activate_recruiter(
    recruiter_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Active le compte recruteur + notifie par WhatsApp si phone disponible."""
    from app.models.recruiter import Recruiter
    from app.services.whatsapp.sender import whatsapp_sender

    result = await db.execute(
        select(Recruiter).where(Recruiter.id == uuid.UUID(recruiter_id))
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Recruteur introuvable")

    r.statut = "active"
    await db.commit()

    if r.phone:
        try:
            await whatsapp_sender.send_text(
                r.phone,
                f"✅ *{r.nom}*, votre compte recruteur *Prepa* est validé !\n\n"
                f"Connectez-vous sur le dashboard pour publier vos annonces.\n"
                f"Votre clé API : `{r.api_key[:12]}...`"
            )
        except Exception as e:
            print(f"Admin activate recruiter WhatsApp: {e}")

    return {"success": True, "statut": "active"}


@router.post("/admin/recruiters/{recruiter_id}/suspend")
async def suspend_recruiter(
    recruiter_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Suspend le compte recruteur."""
    from app.models.recruiter import Recruiter

    result = await db.execute(
        select(Recruiter).where(Recruiter.id == uuid.UUID(recruiter_id))
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Recruteur introuvable")

    r.statut = "suspended"
    await db.commit()
    return {"success": True, "statut": "suspended"}


# ── ANNONCES EMPLOI ────────────────────────────────────────────────────

@router.get("/admin/jobs")
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    statut: str | None = None,
    secteur: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Liste toutes les annonces avec info recruteur."""
    from app.models.job_opportunity import JobOpportunity
    from app.models.recruiter import Recruiter

    q = select(JobOpportunity).order_by(JobOpportunity.created_at.desc())
    if statut:
        q = q.where(JobOpportunity.statut == statut)
    if secteur:
        q = q.where(JobOpportunity.secteur == secteur)
    q = q.limit(limit).offset(offset)

    result = await db.execute(q)
    jobs = result.scalars().all()

    out = []
    for j in jobs:
        recruiter_info = None
        if j.recruiter_id:
            r_res = await db.execute(select(Recruiter).where(Recruiter.id == j.recruiter_id))
            r = r_res.scalar_one_or_none()
            if r:
                recruiter_info = {"id": str(r.id), "nom": r.nom, "entreprise": r.entreprise}
        out.append({
            "id": str(j.id),
            "titre": j.titre,
            "entreprise": j.entreprise,
            "secteur": j.secteur,
            "localisation": j.localisation,
            "type_contrat": j.type_contrat,
            "niveau_etudes": j.niveau_etudes,
            "statut": j.statut,
            "source": j.source,
            "is_featured": j.is_featured,
            "vues": j.vues,
            "nb_notifications_sent": j.nb_notifications_sent or 0,
            "has_embedding": j.embedding is not None,
            "recruiter": recruiter_info,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "expires_at": j.expires_at.isoformat() if j.expires_at else None,
        })
    return out


@router.post("/admin/jobs/import-url")
async def import_job_from_url(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Importe une offre depuis une URL (EmploiDakar ou autre).
    Scrape via Zyte → LLM extrait les champs → doublon → création + embedding + matching.
    """
    from app.models.job_opportunity import JobOpportunity
    from app.services.scraping_service import _extract_job_details_llm, _content_hash, clean_text
    from app.services.rag.embedding_service import embedding_service
    from app.services.matching_service import run_match_all_candidates_bg
    from datetime import timezone, timedelta
    import httpx, asyncio

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps de requête invalide")

    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL manquante")

    # Doublon URL
    existing = (await db.execute(
        select(JobOpportunity).where(JobOpportunity.source_url == url)
    )).scalar_one_or_none()
    if existing:
        return {"success": False, "duplicate": True,
                "message": f"Offre déjà importée ({existing.titre})",
                "existing_id": str(existing.id)}

    # Scrape via Zyte
    description_raw = ""
    if not settings.zyte_api_key:
        return {"success": False, "message": "ZYTE_API_KEY non configurée"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.zyte.com/v1/extract",
                auth=(settings.zyte_api_key, ""),
                json={"url": url, "browserHtml": True},
            )
            html = resp.json().get("browserHtml", "")
            if html:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                meta = soup.find("meta", attrs={"name": "description"})
                if meta:
                    description_raw = meta.get("content", "")
                if not description_raw:
                    # Fallback : texte visible principal
                    body = soup.find("body")
                    if body:
                        description_raw = body.get_text(" ", strip=True)[:2000]
    except Exception as e:
        print(f"import-url Zyte error: {e}")

    description_raw = clean_text(description_raw) or ""
    if not description_raw:
        return {"success": False, "message": "Impossible de lire le contenu de cette URL"}

    detail = await _extract_job_details_llm(description_raw, url)
    if not detail.get("titre"):
        return {"success": False, "message": "Impossible d'extraire les infos de cette URL"}

    annee = datetime.now().year
    c_hash = _content_hash(
        detail.get("titre") or "", detail.get("entreprise") or "",
        detail.get("localisation") or "", annee,
    )
    existing_hash = (await db.execute(
        select(JobOpportunity).where(JobOpportunity.content_hash == c_hash)
    )).scalar_one_or_none()
    if existing_hash:
        return {"success": False, "duplicate": True,
                "message": f"Offre similaire déjà existante ({existing_hash.titre})",
                "existing_id": str(existing_hash.id)}

    now = datetime.now(timezone.utc)
    job = JobOpportunity(
        titre=(clean_text(detail.get("titre")) or "")[:200],
        entreprise=(clean_text(detail.get("entreprise")) or "Non précisé")[:200],
        secteur=(clean_text(detail.get("secteur")) or None),
        localisation=(clean_text(detail.get("localisation")) or "Sénégal")[:200],
        description=description_raw[:500],
        description_complete=description_raw[:3000],
        type_contrat=detail.get("type_contrat"),
        email_candidature=detail.get("email_candidature"),
        taches=detail.get("taches", []),
        conditions_requises=detail.get("conditions_requises", []),
        avantages=detail.get("avantages", []),
        experience_min_annees=detail.get("experience_min_annees"),
        source="admin",
        source_url=url,
        statut="active",
        content_hash=c_hash,
        annee_publication=annee,
        last_seen_at=now,
        expires_at=now + timedelta(days=60),
    )
    db.add(job)
    await db.flush()

    try:
        embed_text = (
            f"{job.titre} {job.entreprise} {job.secteur or ''} "
            f"{job.localisation} {job.type_contrat or ''} {' '.join(job.taches or [])}"
        )
        embedding = await embedding_service.embed_text(embed_text)
        if embedding:
            job.embedding = embedding
    except Exception as e:
        print(f"import-url embedding error: {e}")

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    asyncio.create_task(run_match_all_candidates_bg(job.id))

    return {"success": True, "job": {
        "id": str(job.id), "titre": job.titre, "entreprise": job.entreprise,
        "secteur": job.secteur, "localisation": job.localisation,
        "type_contrat": job.type_contrat, "taches": job.taches,
        "conditions_requises": job.conditions_requises,
    }}


@router.post("/admin/jobs/import-pdf")
async def import_job_from_pdf(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Importe une offre depuis un PDF (fiche de poste).
    Extraction fitz → LLM → stockage PDF → création + embedding + matching.
    """
    from app.models.job_opportunity import JobOpportunity
    from app.services.scraping_service import _extract_job_details_llm, _content_hash, clean_text
    from app.services.rag.embedding_service import embedding_service
    from app.services.matching_service import run_match_all_candidates_bg
    from datetime import timezone, timedelta
    from pathlib import Path
    import base64, hashlib, asyncio, re

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps de requête invalide")

    pdf_b64 = body.get("pdf_b64", "")
    filename = body.get("filename", "offre.pdf")
    if not pdf_b64:
        raise HTTPException(status_code=400, detail="PDF manquant")

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="PDF invalide (base64)")
    if len(pdf_bytes) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF trop volumineux (max 15 MB)")

    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text")
        doc.close()
    except Exception as e:
        return {"success": False, "message": f"Impossible de lire le PDF: {e}"}

    text = clean_text(text) or ""
    if not text.strip():
        return {"success": False, "message": "PDF vide ou illisible"}

    detail = await _extract_job_details_llm(text[:2000], "pdf_upload")
    if not detail.get("titre"):
        return {"success": False, "message": "Impossible d'extraire les infos du PDF"}

    # Stocke le PDF
    try:
        pdf_dir = Path("/home/prepa/app/job_pdfs")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]
        safe_name = re.sub(r"[^\w.\-]", "_", filename)[:60]
        pdf_path = pdf_dir / f"{pdf_hash}_{safe_name}"
        pdf_path.write_bytes(pdf_bytes)
        pdf_path.chmod(0o644)
        pdf_url = f"http://72.62.4.97/job_pdfs/{pdf_path.name}"
    except Exception as e:
        return {"success": False, "message": f"Impossible de stocker le PDF: {e}"}

    annee = datetime.now().year
    c_hash = _content_hash(
        detail.get("titre") or "", detail.get("entreprise") or "",
        detail.get("localisation") or "", annee,
    )
    existing = (await db.execute(
        select(JobOpportunity).where(JobOpportunity.content_hash == c_hash)
    )).scalar_one_or_none()
    if existing:
        return {"success": False, "duplicate": True,
                "message": f"Offre similaire déjà existante ({existing.titre})"}

    now = datetime.now(timezone.utc)
    job = JobOpportunity(
        titre=(clean_text(detail.get("titre")) or "")[:200],
        entreprise=(clean_text(detail.get("entreprise")) or "Non précisé")[:200],
        secteur=(clean_text(detail.get("secteur")) or None),
        localisation=(clean_text(detail.get("localisation")) or "Sénégal")[:200],
        description=text[:500],
        description_complete=text[:3000],
        type_contrat=detail.get("type_contrat"),
        email_candidature=detail.get("email_candidature"),
        taches=detail.get("taches", []),
        conditions_requises=detail.get("conditions_requises", []),
        avantages=detail.get("avantages", []),
        experience_min_annees=detail.get("experience_min_annees"),
        source="admin",
        source_url=pdf_url,
        statut="active",
        content_hash=c_hash,
        annee_publication=annee,
        last_seen_at=now,
        expires_at=now + timedelta(days=60),
    )
    db.add(job)
    await db.flush()

    try:
        embed_text = (
            f"{job.titre} {job.entreprise} {job.secteur or ''} "
            f"{job.localisation} {' '.join(job.taches or [])}"
        )
        embedding = await embedding_service.embed_text(embed_text)
        if embedding:
            job.embedding = embedding
    except Exception as e:
        print(f"import-pdf embedding error: {e}")

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    asyncio.create_task(run_match_all_candidates_bg(job.id))

    return {"success": True, "job": {
        "id": str(job.id), "titre": job.titre, "entreprise": job.entreprise,
        "secteur": job.secteur, "localisation": job.localisation,
        "type_contrat": job.type_contrat, "taches": job.taches,
        "conditions_requises": job.conditions_requises,
    }, "pdf_url": pdf_url, "extracted_text_preview": text[:200]}


@router.post("/admin/jobs")
async def create_job_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Crée une annonce manuellement (statut=active directement).
    Vérifie les doublons, génère l'embedding + lance le matching en arrière-plan.
    """
    from app.models.job_opportunity import JobOpportunity
    from app.services.rag.embedding_service import embedding_service
    from app.services.matching_service import matching_service
    from app.services.scraping_service import scraping_service
    import asyncio

    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Corps de requête invalide: {e}")

    titre = (data.get("titre") or "").strip()
    if not titre:
        raise HTTPException(status_code=400, detail="titre requis")

    # Vérifie doublon AVANT de créer l'objet (pas de flush encore)
    from datetime import datetime as _dt
    duplicate = await scraping_service.check_duplicate_manual(
        db=db,
        titre=titre,
        entreprise=data.get("entreprise") or "",
        localisation=data.get("localisation") or "",
        annee=data.get("annee_publication") or _dt.now().year,
        source_url=data.get("source_url"),
    )
    if duplicate and not data.get("force_publish"):
        return {
            "success": False,
            "duplicate": True,
            "duplicate_info": duplicate,
            "message": duplicate["message"],
        }

    # Traitement des dates optionnelles
    from datetime import timezone as _tz, timedelta as _td
    _now = datetime.now(_tz.utc)

    expires_at = None
    if data.get("expires_at"):
        try:
            from datetime import datetime as _dt
            expires_at = _dt.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        except Exception:
            pass
    if not expires_at:
        expires_at = _now + _td(days=60)  # défaut 60 jours

    date_limite = None
    if data.get("date_limite"):
        try:
            from datetime import datetime as _dt
            date_limite = _dt.fromisoformat(data["date_limite"].replace("Z", "+00:00"))
        except Exception:
            pass

    job = JobOpportunity(
        titre=titre,
        entreprise=(data.get("entreprise") or "").strip(),
        secteur=data.get("secteur"),
        localisation=data.get("localisation"),
        description=data.get("description"),
        competences_requises=data.get("competences_requises"),
        niveau_etudes=data.get("niveau_etudes"),
        type_contrat=data.get("type_contrat"),
        email_candidature=data.get("email_candidature"),
        source="admin",
        statut="active",
        is_featured=bool(data.get("is_featured", False)),
        expires_at=expires_at,
        date_limite=date_limite,
        source_url=data.get("source_url"),
        annee_publication=data.get("annee_publication") or datetime.now().year,
    )
    db.add(job)

    try:
        await db.flush()  # obtenir l'id avant embedding
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    # Génère embedding
    embedding_text = (
        f"{job.titre}. {job.entreprise}. "
        f"Secteur: {job.secteur or ''}. "
        f"Contrat: {job.type_contrat or ''}. "
        f"Niveau: {job.niveau_etudes or ''}. "
        f"{(job.description or '')[:500]}"
    )
    embedding = await embedding_service.embed_text(embedding_text)
    if embedding:
        job.embedding = embedding

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    # Lance matching en arrière-plan (session DB dédiée)
    from app.services.matching_service import run_match_all_candidates_bg
    asyncio.create_task(run_match_all_candidates_bg(job.id))

    return {"success": True, "job_id": str(job.id), "statut": "active", "embedding_generated": embedding is not None}


@router.post("/admin/jobs/{job_id}/validate")
async def validate_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Valide une annonce : pending → active.
    Génère l'embedding + déclenche le matching en arrière-plan.
    """
    from app.models.job_opportunity import JobOpportunity
    from app.services.rag.embedding_service import embedding_service
    from app.services.matching_service import matching_service
    import asyncio

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(select(JobOpportunity).where(JobOpportunity.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    job.statut = "active"

    embedding_text = (
        f"{job.titre}. {job.entreprise}. "
        f"Secteur: {job.secteur or ''}. "
        f"Contrat: {job.type_contrat or ''}. "
        f"Niveau: {job.niveau_etudes or ''}. "
        f"{(job.description or '')[:500]}"
    )
    embedding = await embedding_service.embed_text(embedding_text)
    if embedding:
        job.embedding = embedding
        print(f"  → Embedding annonce {job_id} ✅")

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    # Matching en arrière-plan (session DB dédiée)
    from app.services.matching_service import run_match_all_candidates_bg
    asyncio.create_task(run_match_all_candidates_bg(job.id))

    return {"success": True, "embedding_generated": embedding is not None}


@router.post("/admin/jobs/{job_id}/expire")
async def expire_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Expire manuellement une annonce."""
    from app.models.job_opportunity import JobOpportunity

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(select(JobOpportunity).where(JobOpportunity.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    job.statut = "expired"
    await db.commit()
    return {"success": True, "statut": "expired"}


@router.delete("/admin/jobs/{job_id}")
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Supprime une annonce définitivement."""
    from app.models.job_opportunity import JobOpportunity

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(select(JobOpportunity).where(JobOpportunity.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    await db.delete(job)
    await db.commit()
    return {"success": True}


@router.get("/admin/jobs/stats")
async def jobs_stats(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Statistiques globales emploi :
    - Nombre d'offres par statut
    - Total matchs envoyés (et cette semaine)
    - Top 5 offres par nb_notifications_sent
    """
    from app.models.job_opportunity import JobOpportunity
    from app.models.candidate_profile import JobMatch
    from sqlalchemy import func as sqlfunc
    from datetime import timezone, timedelta

    # Comptes par statut
    statuts_res = await db.execute(
        select(JobOpportunity.statut, sqlfunc.count().label("nb"))
        .group_by(JobOpportunity.statut)
    )
    statuts = {row.statut: row.nb for row in statuts_res}

    # Total matchs
    total_matchs_res = await db.execute(select(sqlfunc.count()).select_from(JobMatch))
    total_matchs = total_matchs_res.scalar() or 0

    # Matchs cette semaine
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    week_res = await db.execute(
        select(sqlfunc.count()).select_from(JobMatch)
        .where(JobMatch.created_at >= week_ago)
    )
    matchs_semaine = week_res.scalar() or 0

    # Top 5 offres par notifications envoyées
    top_res = await db.execute(
        select(JobOpportunity)
        .where(JobOpportunity.nb_notifications_sent > 0)
        .order_by(JobOpportunity.nb_notifications_sent.desc())
        .limit(5)
    )
    top_jobs = top_res.scalars().all()

    return {
        "statuts": statuts,
        "total_matchs": total_matchs,
        "matchs_semaine": matchs_semaine,
        "top_offres": [
            {
                "id": str(j.id),
                "titre": j.titre,
                "entreprise": j.entreprise,
                "nb_notifications_sent": j.nb_notifications_sent,
                "statut": j.statut,
            }
            for j in top_jobs
        ],
    }


@router.get("/admin/jobs/{job_id}/detail")
async def get_job_detail(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Retourne tous les champs d'une offre (vue détaillée)."""
    from app.models.job_opportunity import JobOpportunity

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(select(JobOpportunity).where(JobOpportunity.id == job_uuid))
    j = result.scalar_one_or_none()
    if not j:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    return {
        "id": str(j.id),
        "titre": j.titre,
        "entreprise": j.entreprise,
        "secteur": j.secteur,
        "localisation": j.localisation,
        "type_contrat": j.type_contrat,
        "niveau_etudes": j.niveau_etudes,
        "experience_min_annees": j.experience_min_annees,
        "description": j.description,
        "description_complete": j.description_complete,
        "taches": j.taches or [],
        "conditions_requises": j.conditions_requises or [],
        "competences_requises": j.competences_requises or [],
        "avantages": j.avantages or [],
        "email_candidature": j.email_candidature,
        "source_url": j.source_url,
        "source": j.source,
        "statut": j.statut,
        "is_featured": j.is_featured,
        "vues": j.vues,
        "nb_notifications_sent": j.nb_notifications_sent,
        "has_embedding": j.embedding is not None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "expires_at": j.expires_at.isoformat() if j.expires_at else None,
        "date_limite": j.date_limite.isoformat() if j.date_limite else None,
        "logo_url": j.logo_url,
    }


@router.get("/admin/jobs/{job_id}/matches")
async def get_job_matches(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    limit: int = 100,
    offset: int = 0,
):
    """Liste les candidats matchés pour une offre donnée avec leur score et statut."""
    from app.models.candidate_profile import JobMatch
    from app.models.user import User

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(
        select(JobMatch, User)
        .join(User, User.id == JobMatch.user_id)
        .where(JobMatch.job_id == job_uuid)
        .order_by(JobMatch.score_match.desc())
        .limit(limit).offset(offset)
    )
    rows = result.all()

    total_res = await db.execute(
        select(func.count()).select_from(JobMatch).where(JobMatch.job_id == job_uuid)
    )
    total = total_res.scalar() or 0

    return {
        "total": total,
        "matches": [
            {
                "match_id": str(m.id),
                "user_id": str(u.id),
                "nom": u.name or "—",
                "phone": u.phone_number,
                "plan": u.plan,
                "score_match": round(m.score_match, 2),
                "statut": m.statut,
                "notifie_at": m.notifie_at.isoformat() if m.notifie_at else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m, u in rows
        ],
    }


@router.post("/admin/jobs/{job_id}/rematch")
async def rematch_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Relance le matching d'une offre contre tous les candidats.
    Utile si l'offre existait avant l'inscription de nouveaux candidats.
    """
    from app.models.job_opportunity import JobOpportunity
    from app.services.matching_service import run_match_all_candidates_bg
    import asyncio

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(select(JobOpportunity).where(JobOpportunity.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    if job.statut != "active":
        raise HTTPException(status_code=400, detail="Seules les offres actives peuvent être re-matchées")

    if not job.embedding:
        raise HTTPException(status_code=400, detail="Offre sans embedding — validez-la d'abord")

    asyncio.create_task(run_match_all_candidates_bg(job.id))

    return {"success": True, "message": f"Matching relancé pour « {job.titre} »"}


# ── CANDIDATS ─────────────────────────────────────────────────────────

@router.get("/admin/candidates")
async def list_candidates(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    limit: int = 100,
    offset: int = 0,
):
    """Liste les CandidateProfile avec infos User et nb matchs."""
    from app.models.candidate_profile import CandidateProfile, JobMatch
    from app.models.user import User

    result = await db.execute(
        select(CandidateProfile, User)
        .join(User, User.id == CandidateProfile.user_id)
        .order_by(CandidateProfile.created_at.desc())
        .limit(limit).offset(offset)
    )
    rows = result.all()

    out = []
    for cp, u in rows:
        nb_matchs = await db.scalar(
            select(func.count(JobMatch.id)).where(JobMatch.user_id == cp.user_id)
        ) or 0
        out.append({
            "user_id": str(cp.user_id),
            "name": u.name,
            "phone": u.phone_number,
            "secteurs": cp.secteurs_interets or [],
            "niveau_etudes": cp.niveau_etudes,
            "localisation": cp.localisation,
            "annees_experience": cp.annees_experience,
            "has_cv": bool(cp.cv_url),
            "has_embedding": bool(cp.embedding),
            "nb_matchs": nb_matchs,
            "created_at": cp.created_at.isoformat() if cp.created_at else None,
        })
    return out


@router.post("/admin/candidates/{user_id}/rematch")
async def rematch_candidate(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Relance le matching pour un candidat spécifique."""
    from app.services.matching_service import matching_service

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id invalide")

    try:
        matches = await matching_service.match_candidate(db, uid)
        await db.commit()
        return {"success": True, "matches": len(matches)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur matching: {e}")


@router.post("/admin/users/{user_id}/flush-queue")
async def flush_user_queue(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Force l'envoi immédiat des messages en queue d'un user
    et nettoie son conversation_state si bloqué.
    """
    from app.models.user import User
    from app.services.queue_service import flush_queue

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id invalide")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User introuvable")

    queue_size = len(user.notification_queue or [])
    conv_state = user.conversation_state or {}
    was_busy = bool(
        conv_state.get("awaiting_simulation_copy") or
        conv_state.get("awaiting_copy") or
        conv_state.get("exercise_path") or
        conv_state.get("awaiting_copy_for_free_correction")
    )

    # Nettoie l'état bloqué
    if was_busy:
        user.conversation_state = {}
        await db.flush()

    # Flush la queue
    sent = await flush_queue(db, user)
    await db.commit()

    return {
        "success": True,
        "queue_size": queue_size,
        "sent": sent,
        "state_cleared": was_busy,
    }


@router.post("/admin/users/flush-all-queues")
async def flush_all_queues(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Flush les queues de tous les users ayant des messages en attente.
    Nettoie aussi les conversation_state bloqués.
    """
    from app.models.user import User
    from app.services.queue_service import flush_queue
    from sqlalchemy import func as sqlfunc

    result = await db.execute(
        select(User).where(
            User.notification_queue.isnot(None),
            func.jsonb_array_length(User.notification_queue) > 0,
        )
    )
    users = result.scalars().all()

    total_sent = 0
    cleared = 0
    for user in users:
        conv = user.conversation_state or {}
        if any(conv.get(k) for k in ["awaiting_simulation_copy", "awaiting_copy", "exercise_path", "awaiting_copy_for_free_correction"]):
            user.conversation_state = {}
            await db.flush()
            cleared += 1
        sent = await flush_queue(db, user)
        total_sent += sent

    await db.commit()
    return {
        "success": True,
        "users_processed": len(users),
        "messages_sent": total_sent,
        "states_cleared": cleared,
    }


# ── SCRAPING ──────────────────────────────────────────────────────────

@router.post("/admin/scraping/run")
async def run_scraping_now(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Lance le scraping immédiatement (hors scheduler)."""
    from app.services.scraping_service import scraping_service
    try:
        result = await scraping_service.scrape_all(db)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur scraping: {e}")


# ── PETITS JOBS ───────────────────────────────────────────────────────

@router.get("/admin/petit-jobs")
async def list_petit_jobs(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.petit_job import PetitJob
    from sqlalchemy import select
    result = await db.execute(select(PetitJob).order_by(PetitJob.created_at.desc()))
    jobs = result.scalars().all()
    return [
        {
            "id": str(j.id),
            "titre": j.titre,
            "type_travail": j.type_travail,
            "lieu": j.lieu,
            "date_debut_str": j.date_debut_str,
            "duree": j.duree,
            "remuneration": j.remuneration,
            "nb_postes": j.nb_postes,
            "statut": j.statut,
            "nb_candidats_notifies": j.nb_candidats_notifies,
            "description": j.description,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "expires_at": j.expires_at.isoformat() if j.expires_at else None,
        }
        for j in jobs
    ]


class PetitJobCreate(BaseModel):
    titre: str
    type_travail: str | None = None
    lieu: str | None = None
    date_debut_str: str | None = None
    duree: str | None = None
    remuneration: str | None = None
    nb_postes: int = 1
    offreur_phone: str | None = None
    description: str | None = None


@router.post("/admin/petit-jobs", status_code=201)
async def create_petit_job(
    body: PetitJobCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.petit_job import PetitJob
    from app.services.petit_job_service import PetitJobService
    job = PetitJob(
        employeur_user_id=None,
        titre=body.titre,
        type_travail=body.type_travail,
        lieu=body.lieu,
        date_debut_str=body.date_debut_str,
        duree=body.duree,
        remuneration=body.remuneration,
        nb_postes=body.nb_postes,
        offreur_phone=body.offreur_phone,
        description=body.description,
        statut="ouvert",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    # Notifier les candidats matchés
    try:
        svc = PetitJobService()
        nb = await svc.notify_candidates(db, job)
        job.nb_candidats_notifies = nb
        await db.commit()
    except Exception:
        pass
    return {"id": str(job.id), "nb_candidats_notifies": job.nb_candidats_notifies}


@router.post("/admin/petit-jobs/{job_id}/expire")
async def expire_petit_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.petit_job import PetitJob
    from sqlalchemy import select
    result = await db.execute(select(PetitJob).where(PetitJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Petit job introuvable")
    job.statut = "expiré"
    await db.commit()
    return {"success": True}


@router.delete("/admin/petit-jobs/{job_id}")
async def delete_petit_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.petit_job import PetitJob
    from sqlalchemy import select
    result = await db.execute(select(PetitJob).where(PetitJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Petit job introuvable")
    await db.delete(job)
    await db.commit()
    return {"success": True}


# ── DASHBOARD ─────────────────────────────────────────────────────────

@router.get("/admin", response_class=FileResponse)
async def admin_dashboard():
    return FileResponse("app/static/admin.html")
