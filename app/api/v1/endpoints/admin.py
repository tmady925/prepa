from datetime import date, datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date
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
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
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
    from app.services.rag.indexing_service import indexing_service
    import base64

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Fichier trop grand (max 10MB).")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Requête invalide")

    filename = data.get("filename", "document")
    file_b64 = data.get("file_b64", "")
    title = data.get("title", filename)
    exam_type = data.get("exam_type") or None
    series = data.get("series") or None
    subject = data.get("subject") or None
    doc_type = data.get("doc_type", "cours")

    if not file_b64:
        raise HTTPException(status_code=400, detail="Fichier manquant")

    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Fichier invalide")

    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Fichier trop grand (max 10MB).")

    result = await indexing_service.index_document(
        db=db,
        file_bytes=file_bytes,
        filename=filename,
        title=title,
        exam_type=exam_type,
        series=series,
        subject=subject,
        doc_type=doc_type,
        uploaded_by="admin",
    )
    return result


@router.get("/admin/documents")
async def get_documents(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.rag.indexing_service import indexing_service
    return await indexing_service.get_documents(db)


@router.delete("/admin/documents/{document_id}")
async def delete_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.rag.indexing_service import indexing_service
    success = await indexing_service.delete_document(db, document_id)
    if not success:
        raise HTTPException(status_code=404, detail="Document non trouvé")
    return {"status": "ok"}


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


# ── DASHBOARD ─────────────────────────────────────────────────────────

@router.get("/admin", response_class=FileResponse)
async def admin_dashboard():
    return FileResponse("app/static/admin.html")
