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


@router.post("/admin/documents/upload-exercises")
async def upload_exercise_document(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Upload un document pédagogique, analyse les exercices,
    génère les PDFs et les corrections.
    """
    from app.services.document_analyzer_service import document_analyzer_service
    from app.services.exercise_extractor_service import exercise_extractor_service
    from app.models.exercise import Exercise
    import base64

    data = await request.json()
    file_b64 = data.get("file_b64")
    filename = data.get("filename", "document.pdf")
    exam_type = data.get("exam_type")
    serie = data.get("serie")
    matiere = data.get("matiere")

    if not file_b64:
        raise HTTPException(status_code=400, detail="file_b64 requis")

    # Décode le fichier
    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="file_b64 invalide")

    print(f"Analyse document: {filename} ({len(file_bytes)} bytes)")

    # 1. Analyse du document
    analysis = await document_analyzer_service.analyze_document(
        file_bytes=file_bytes,
        filename=filename,
        exam_type=exam_type,
        matiere=matiere,
        serie=serie,
    )

    if not analysis.get("success"):
        raise HTTPException(status_code=422, detail=analysis.get("error"))

    exercises_detected = analysis.get("exercises", [])
    if not exercises_detected:
        return {
            "success": False,
            "error": "Aucun exercice détecté dans le document",
            "analysis": analysis,
        }

    print(f"  → {len(exercises_detected)} exercices détectés")

    # 2. Traite chaque exercice
    results = []
    saved_exercises = []

    for ex_data in exercises_detected:
        print(f"\n  Traitement exercice {ex_data.get('number')}...")

        # Extrait et génère les PDFs
        result = await exercise_extractor_service.process_exercise(
            file_bytes=file_bytes,
            exercise_data=ex_data,
            source_filename=filename,
            matiere=analysis.get("matiere") or matiere or "autre",
            exam_type=analysis.get("exam_type") or exam_type or "bac_senegal",
            serie=analysis.get("serie") or serie,
            annee=analysis.get("annee"),
        )

        # Sauvegarde en DB
        exercise = Exercise(
            source_filename=filename,
            title=ex_data.get("title"),
            exam_type=analysis.get("exam_type") or exam_type,
            serie=analysis.get("serie") or serie,
            matiere=analysis.get("matiere") or matiere,
            chapitre=ex_data.get("chapitre"),
            niveau=ex_data.get("niveau", 2),
            annee=analysis.get("annee"),
            tags=ex_data.get("tags"),
            bareme=ex_data.get("bareme"),
            exercise_number=ex_data.get("number"),
            page_debut=ex_data.get("page_debut"),
            page_fin=ex_data.get("page_fin"),
            exercise_path=result.get("exercise_path"),
            correction_path=result.get("correction_path"),
            correction_generated=result.get("correction_generated", False),
            status="ready" if result.get("exercise_path") else "error",
            error_message=result.get("error"),
        )
        db.add(exercise)
        saved_exercises.append(exercise)
        results.append(result)

    await db.commit()

    print(f"\n✅ {len(saved_exercises)} exercices traités et sauvegardés")

    return {
        "success": True,
        "filename": filename,
        "exercises_count": len(saved_exercises),
        "matiere": analysis.get("matiere"),
        "exam_type": analysis.get("exam_type"),
        "serie": analysis.get("serie"),
        "annee": analysis.get("annee"),
        "exercises": [
            {
                "number": r["exercise_number"],
                "title": r["title"],
                "exercise_path": r["exercise_path"],
                "correction_path": r["correction_path"],
                "correction_generated": r["correction_generated"],
                "error": r.get("error"),
            }
            for r in results
        ],
    }


@router.post("/admin/exercises/upload")
async def upload_exercise(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Upload manuel d'un exercice PDF + correction PDF optionnelle.
    Le niveau est déterminé automatiquement par Mistral.
    """
    import base64
    import uuid
    import json
    import re
    import httpx
    from pathlib import Path
    from app.models.exercise import Exercise

    data = await request.json()
    exercise_b64 = data.get("exercise_b64")
    correction_b64 = data.get("correction_b64")
    filename_ex = data.get("filename_ex", "exercice.pdf")
    filename_corr = data.get("filename_corr", "correction.pdf")
    matiere = data.get("matiere")
    exam_type = data.get("exam_type", "bac_senegal")
    serie = data.get("serie")
    chapitre = data.get("chapitre")
    annee = data.get("annee")
    tags = data.get("tags", [])
    title = data.get("title", "")

    if not exercise_b64:
        raise HTTPException(status_code=400, detail="exercise_b64 requis")
    if not matiere:
        raise HTTPException(status_code=400, detail="matiere requise")

    # Décode les fichiers
    try:
        exercise_bytes = base64.b64decode(exercise_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="exercise_b64 invalide")

    correction_bytes = None
    if correction_b64:
        try:
            correction_bytes = base64.b64decode(correction_b64)
        except Exception:
            pass

    # Détermine le niveau automatiquement avec Mistral
    niveau = 2  # défaut
    try:
        import fitz
        doc = fitz.open(stream=exercise_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text")
        doc.close()
        text = text[:3000]

        if text.strip():
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{
                            "role": "user",
                            "content": f"""Analyse cet exercice scolaire sénégalais et détermine son niveau de difficulté.

Matière : {matiere}
Chapitre : {chapitre or 'général'}
Examen : {exam_type} {serie or ''}

Exercice :
---
{text}
---

Retourne UNIQUEMENT un JSON :
{{"niveau": 1, "justification": "raison courte"}}

niveau 1 = facile (élève moyen peut résoudre seul)
niveau 2 = intermédiaire (nécessite bonne maîtrise du cours)
niveau 3 = difficile (demande réflexion avancée, type concours)"""
                        }],
                        "temperature": 0.1,
                        "max_tokens": 100,
                    },
                )
                result = response.json()
                if "choices" in result:
                    raw = result["choices"][0]["message"]["content"].strip()
                    raw = re.sub(r"```json|```", "", raw).strip()
                    parsed = json.loads(raw)
                    niveau = int(parsed.get("niveau", 2))
                    print(f"  → Niveau détecté : {niveau} — {parsed.get('justification', '')}")
    except Exception as e:
        print(f"  → Erreur détection niveau: {e} — défaut niveau 2")

    # Sauvegarde les fichiers
    EXERCISES_DIR = Path("/home/prepa/app/exercises")
    CORRECTIONS_DIR = Path("/home/prepa/app/corrections")

    ex_folder = EXERCISES_DIR / matiere
    ex_folder.mkdir(parents=True, exist_ok=True)

    serie_clean = re.sub(r'[^A-Z0-9]', '', (serie or 'gen').upper())
    base_name = f"{exam_type}_{serie_clean}_{matiere}_{chapitre or 'general'}"
    if annee:
        base_name += f"_{annee}"
    base_name += f"_{uuid.uuid4().hex[:6]}"

    # Sauvegarde exercice
    ex_path = ex_folder / f"{base_name}.pdf"
    ex_path.write_bytes(exercise_bytes)
    ex_path.chmod(0o644)
    print(f"  → Exercice sauvegardé : {ex_path}")

    # Sauvegarde correction si présente
    corr_path = None
    if correction_bytes:
        corr_folder = CORRECTIONS_DIR / matiere
        corr_folder.mkdir(parents=True, exist_ok=True)
        corr_path = corr_folder / f"{base_name}_correction.pdf"
        corr_path.write_bytes(correction_bytes)
        corr_path.chmod(0o644)
        print(f"  → Correction sauvegardée : {corr_path}")

    # Sauvegarde en DB
    exercise = Exercise(
        title=title or base_name,
        exam_type=exam_type,
        serie=serie,
        matiere=matiere,
        chapitre=chapitre,
        niveau=niveau,
        annee=int(annee) if annee else None,
        tags=tags if tags else None,
        exercise_path=str(ex_path),
        correction_path=str(corr_path) if corr_path else None,
        correction_generated=False,
        status="ready",
    )
    db.add(exercise)
    await db.commit()

    return {
        "success": True,
        "id": str(exercise.id),
        "title": exercise.title,
        "niveau": niveau,
        "matiere": matiere,
        "exam_type": exam_type,
        "serie": serie,
        "chapitre": chapitre,
        "exercise_path": str(ex_path),
        "correction_path": str(corr_path) if corr_path else None,
    }


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
