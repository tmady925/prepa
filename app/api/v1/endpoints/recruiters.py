"""
Endpoints recruteurs — Sprint 2.
Auth : X-Api-Key header.
Plans : gratuit (2 annonces) / starter / pro.
"""
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.database import get_db
from app.models.recruiter import Recruiter
from app.models.job_opportunity import JobOpportunity
from app.core.settings import get_settings

settings = get_settings()
router = APIRouter()


# ── Helper : hash password ─────────────────────────────────────────────

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── Middleware auth recruteur ──────────────────────────────────────────

async def get_recruiter(
    x_api_key: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> Recruiter:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key manquante")
    result = await db.execute(
        select(Recruiter).where(Recruiter.api_key == x_api_key)
    )
    recruiter = result.scalar_one_or_none()
    if not recruiter:
        raise HTTPException(status_code=401, detail="API key invalide")
    if recruiter.statut == "pending":
        raise HTTPException(status_code=403, detail="Compte en attente de validation par l'admin")
    if recruiter.statut == "suspended":
        raise HTTPException(status_code=403, detail="Compte suspendu")
    return recruiter


# ── Inscription ────────────────────────────────────────────────────────

@router.post("/recruiters/register")
async def register_recruiter(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Inscription recruteur.
    Statut=pending jusqu'à validation admin.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps de requête invalide")

    nom = (data.get("nom") or "").strip()
    entreprise = (data.get("entreprise") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip() or None
    password = data.get("password") or ""

    if not nom:
        raise HTTPException(status_code=400, detail="nom requis")
    if not entreprise:
        raise HTTPException(status_code=400, detail="entreprise requise")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="email invalide")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password trop court (min 6 caractères)")

    # Vérifie email unique
    existing = await db.execute(select(Recruiter).where(Recruiter.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email déjà utilisé")

    recruiter = Recruiter(
        nom=nom,
        entreprise=entreprise,
        email=email,
        phone=phone,
        password_hash=_hash_password(password),
        plan="gratuit",
        annonces_restantes=2,
        statut="pending",
        api_key=secrets.token_hex(32),
    )
    db.add(recruiter)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    return {
        "success": True,
        "recruiter_id": str(recruiter.id),
        "message": "Compte créé, en attente de validation par l'équipe Prepa.",
    }


# ── Connexion ──────────────────────────────────────────────────────────

@router.post("/recruiters/login")
async def login_recruiter(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Connexion recruteur — retourne l'api_key si actif."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps de requête invalide")

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    result = await db.execute(select(Recruiter).where(Recruiter.email == email))
    recruiter = result.scalar_one_or_none()

    if not recruiter or not recruiter.password_hash:
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    if recruiter.password_hash != _hash_password(password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    if recruiter.statut == "pending":
        return {
            "success": False,
            "statut": "pending",
            "message": "Votre compte est en attente de validation par l'équipe Prepa. Vous serez notifié sous 24h.",
        }
    if recruiter.statut == "suspended":
        raise HTTPException(status_code=403, detail="Compte suspendu. Contactez le support.")

    return {
        "success": True,
        "api_key": recruiter.api_key,
        "recruiter": {
            "id": str(recruiter.id),
            "nom": recruiter.nom,
            "entreprise": recruiter.entreprise,
            "plan": recruiter.plan,
            "statut": recruiter.statut,
            "annonces_restantes": recruiter.annonces_restantes,
        },
    }


# ── Profil ─────────────────────────────────────────────────────────────

@router.get("/recruiters/me")
async def get_recruiter_me(
    recruiter: Recruiter = Depends(get_recruiter),
    db: AsyncSession = Depends(get_db),
):
    """Profil recruteur + nb annonces actives + quota restant."""
    active_jobs = await db.scalar(
        select(func.count(JobOpportunity.id)).where(
            JobOpportunity.recruiter_id == recruiter.id,
            JobOpportunity.statut == "active",
        )
    ) or 0
    pending_jobs = await db.scalar(
        select(func.count(JobOpportunity.id)).where(
            JobOpportunity.recruiter_id == recruiter.id,
            JobOpportunity.statut == "pending",
        )
    ) or 0

    return {
        "id": str(recruiter.id),
        "nom": recruiter.nom,
        "entreprise": recruiter.entreprise,
        "email": recruiter.email,
        "phone": recruiter.phone,
        "plan": recruiter.plan,
        "statut": recruiter.statut,
        "annonces_restantes": recruiter.annonces_restantes,
        "abonnement_expire_at": recruiter.abonnement_expire_at.isoformat() if recruiter.abonnement_expire_at else None,
        "logo_url": recruiter.logo_url,
        "annonces_actives": active_jobs,
        "annonces_en_attente": pending_jobs,
    }


# ── Publier une annonce ────────────────────────────────────────────────

@router.post("/recruiters/jobs")
async def create_job(
    request: Request,
    recruiter: Recruiter = Depends(get_recruiter),
    db: AsyncSession = Depends(get_db),
):
    """
    Crée une annonce (statut=pending, validation admin requise).
    Plan gratuit : 2 annonces max, expiration 15 jours.
    Plan starter/pro : illimité, expiration 30 jours.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps de requête invalide")

    titre = (data.get("titre") or "").strip()
    if not titre:
        raise HTTPException(status_code=400, detail="titre requis")

    # Vérifie quota
    if recruiter.plan == "gratuit" and recruiter.annonces_restantes <= 0:
        raise HTTPException(
            status_code=403,
            detail="Limite atteinte. Passez au plan Starter ou Pro pour publier plus d'annonces.",
        )

    # Durée selon plan
    duree = 30 if recruiter.plan in ("starter", "pro") else 15
    expires = datetime.now(timezone.utc) + timedelta(days=duree)

    job = JobOpportunity(
        recruiter_id=recruiter.id,
        titre=titre,
        entreprise=data.get("entreprise") or recruiter.entreprise,
        secteur=data.get("secteur"),
        localisation=data.get("localisation"),
        description=data.get("description"),
        competences_requises=data.get("competences_requises"),
        niveau_etudes=data.get("niveau_etudes"),
        type_contrat=data.get("type_contrat"),
        email_candidature=data.get("email_candidature") or recruiter.email,
        logo_url=recruiter.logo_url,
        source="recruteur",
        statut="pending",
        expires_at=expires,
    )
    db.add(job)

    # Décrémente quota gratuit
    if recruiter.plan == "gratuit":
        recruiter.annonces_restantes = max(0, recruiter.annonces_restantes - 1)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    return {
        "success": True,
        "job_id": str(job.id),
        "message": "Annonce soumise, en attente de validation par l'équipe Prepa.",
        "expires_at": expires.isoformat(),
    }


# ── Liste annonces du recruteur ────────────────────────────────────────

@router.get("/recruiters/jobs")
async def list_recruiter_jobs(
    recruiter: Recruiter = Depends(get_recruiter),
    db: AsyncSession = Depends(get_db),
):
    """Liste les annonces du recruteur avec stats basiques."""
    result = await db.execute(
        select(JobOpportunity)
        .where(JobOpportunity.recruiter_id == recruiter.id)
        .order_by(JobOpportunity.created_at.desc())
    )
    jobs = result.scalars().all()

    return [
        {
            "id": str(j.id),
            "titre": j.titre,
            "secteur": j.secteur,
            "localisation": j.localisation,
            "type_contrat": j.type_contrat,
            "niveau_etudes": j.niveau_etudes,
            "statut": j.statut,
            "vues": j.vues,
            "is_featured": j.is_featured,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "expires_at": j.expires_at.isoformat() if j.expires_at else None,
        }
        for j in jobs
    ]


# ── Expirer une annonce ────────────────────────────────────────────────

@router.delete("/recruiters/jobs/{job_id}")
async def expire_recruiter_job(
    job_id: str,
    recruiter: Recruiter = Depends(get_recruiter),
    db: AsyncSession = Depends(get_db),
):
    """Expire l'annonce (ownership vérifié)."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="job_id invalide")

    result = await db.execute(
        select(JobOpportunity).where(
            JobOpportunity.id == job_uuid,
            JobOpportunity.recruiter_id == recruiter.id,
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Annonce introuvable")

    job.statut = "expired"
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

    return {"success": True, "message": "Annonce expirée"}


# ── Abonnement recruteur ───────────────────────────────────────────────

@router.post("/recruiters/subscribe")
async def recruiter_subscribe(
    request: Request,
    recruiter: Recruiter = Depends(get_recruiter),
    db: AsyncSession = Depends(get_db),
):
    """
    Crée un lien de paiement PayDunya pour un plan recruteur.
    starter = 15 000 FCFA / pro = 45 000 FCFA.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    plan = data.get("plan", "starter")
    _PLANS = {
        "starter": {"label": "Recruteur Starter", "amount": 15000},
        "pro":     {"label": "Recruteur Pro",     "amount": 45000},
    }
    if plan not in _PLANS:
        raise HTTPException(status_code=400, detail=f"plan invalide. Valeurs : {list(_PLANS)}")

    plan_info = _PLANS[plan]

    import httpx
    _base = (
        "https://app.paydunya.com/api/v1"
        if settings.paydunya_mode == "live"
        else "https://app.paydunya.com/sandbox-api/v1"
    )
    _headers = {
        "PAYDUNYA-MASTER-KEY": settings.paydunya_master_key,
        "PAYDUNYA-PRIVATE-KEY": settings.paydunya_private_key,
        "PAYDUNYA-TOKEN": settings.paydunya_token,
        "Content-Type": "application/json",
    }
    payload = {
        "invoice": {
            "items": {
                "item_0": {
                    "name": plan_info["label"],
                    "quantity": 1,
                    "unit_price": str(plan_info["amount"]),
                    "total_price": str(plan_info["amount"]),
                    "description": f"{plan_info['label']} — 1 mois",
                }
            },
            "taxes": {},
            "total_amount": str(plan_info["amount"]),
            "description": f"{plan_info['label']} pour {recruiter.entreprise}",
        },
        "store": {"name": "Prepa"},
        "custom_data": {
            "recruiter_id": str(recruiter.id),
            "plan": plan,
            "type": "recruiter",
        },
        "actions": {
            "cancel_url":   f"{settings.app_base_url}/recruiters/dashboard",
            "return_url":   f"{settings.app_base_url}/recruiters/dashboard?payment=success",
            "callback_url": f"{settings.app_base_url}/api/v1/payments/recruiter-ipn",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_base}/checkout-invoice/create",
                headers=_headers,
                json=payload,
            )
            result_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erreur PayDunya: {e}")

    if result_data.get("response_code") == "00":
        token = result_data.get("token", "")
        payment_url = f"https://app.paydunya.com/checkout/v1/gateway/receipt/{token}"
        recruiter.paydunya_token = token
        await db.commit()
        return {"success": True, "payment_url": payment_url, "plan": plan}

    return {
        "success": False,
        "error": result_data.get("response_text", "Erreur PayDunya inconnue"),
    }
