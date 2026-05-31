import uuid
from datetime import date, datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date
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
    """Liste les exercices avec filtres optionnels."""
    from app.models.exercise import Exercise

    query = select(Exercise).order_by(Exercise.created_at.desc())

    if matiere:
        query = query.where(Exercise.matiere == matiere)
    if niveau is not None:
        query = query.where(Exercise.niveau == niveau)
    if exam_type:
        query = query.where(Exercise.exam_type == exam_type)

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    exercises = result.scalars().all()

    return [
        {
            "id": str(e.id),
            "title": e.title,
            "exam_type": e.exam_type,
            "serie": e.serie,
            "matiere": e.matiere,
            "chapitre": e.chapitre,
            "niveau": e.niveau,
            "annee": e.annee,
            "tags": e.tags,
            "exercise_path": e.exercise_path,
            "correction_path": e.correction_path,
            "correction_generated": e.correction_generated,
            "status": e.status,
            "error_message": e.error_message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in exercises
    ]


@router.delete("/admin/exercises/{exercise_id}")
async def delete_exercise(
    exercise_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Supprime un exercice (DB + fichiers sur disque)."""
    import uuid
    from pathlib import Path
    from app.models.exercise import Exercise

    try:
        uid = uuid.UUID(exercise_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID invalide")

    result = await db.execute(select(Exercise).where(Exercise.id == uid))
    exercise = result.scalar_one_or_none()
    if not exercise:
        raise HTTPException(status_code=404, detail="Exercice non trouvé")

    # Supprime les fichiers sur disque si présents
    for path_str in [exercise.exercise_path, exercise.correction_path]:
        if path_str:
            p = Path(path_str)
            if p.exists():
                try:
                    p.unlink()
                    print(f"  → Fichier supprimé : {p}")
                except Exception as e:
                    print(f"  → Erreur suppression fichier {p}: {e}")

    await db.delete(exercise)
    await db.commit()
    return {"status": "ok", "id": exercise_id}


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


# ── EXAMENS ────────────────────────────────────────────────────────────

@router.get("/admin/exams")
async def get_exams(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Exam
    result = await db.execute(sa_select(Exam).order_by(Exam.pays, Exam.name))
    exams = result.scalars().all()
    return [
        {
            "id": str(e.id), "code": e.code, "name": e.name,
            "pays": e.pays, "niveau": e.niveau,
            "description": e.description, "is_active": e.is_active,
        }
        for e in exams
    ]


@router.post("/admin/exams")
async def create_exam(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Exam
    data = await request.json()
    exam = Exam(
        code=data["code"],
        name=data["name"],
        pays=data.get("pays", "senegal"),
        niveau=data.get("niveau", "bac"),
        description=data.get("description"),
        is_active=data.get("is_active", True),
    )
    db.add(exam)
    await db.commit()
    return {"success": True, "id": str(exam.id), "code": exam.code}


@router.put("/admin/exams/{exam_id}")
async def update_exam(
    exam_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Exam
    data = await request.json()
    result = await db.execute(sa_select(Exam).where(Exam.id == uuid.UUID(exam_id)))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Examen non trouvé")
    for field in ["name", "pays", "niveau", "description", "is_active"]:
        if field in data:
            setattr(exam, field, data[field])
    await db.commit()
    return {"success": True}


@router.delete("/admin/exams/{exam_id}")
async def delete_exam(
    exam_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Exam
    result = await db.execute(sa_select(Exam).where(Exam.id == uuid.UUID(exam_id)))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Examen non trouvé")
    await db.delete(exam)
    await db.commit()
    return {"success": True}


# ── SÉRIES ─────────────────────────────────────────────────────────────

@router.get("/admin/series")
async def get_series(
    exam_id: str = None,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Series, Exam
    query = (
        sa_select(Series, Exam.name.label("exam_name"), Exam.code.label("exam_code"))
        .outerjoin(Exam, Series.exam_id == Exam.id)
        .order_by(Exam.name, Series.code)
    )
    if exam_id:
        query = query.where(Series.exam_id == uuid.UUID(exam_id))
    result = await db.execute(query)
    rows = result.all()
    return [
        {
            "id": str(s.id), "code": s.code, "name": s.name,
            "exam_id": str(s.exam_id) if s.exam_id else None,
            "exam_name": exam_name, "exam_code": exam_code,
            "description": s.description,
            "matieres": s.matieres,
            "is_active": s.is_active,
        }
        for s, exam_name, exam_code in rows
    ]


@router.post("/admin/series")
async def create_series(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Series
    data = await request.json()
    series = Series(
        code=data["code"],
        name=data["name"],
        exam_id=uuid.UUID(data["exam_id"]) if data.get("exam_id") else None,
        description=data.get("description"),
        matieres=data.get("matieres"),
        is_active=data.get("is_active", True),
    )
    db.add(series)
    await db.commit()
    return {"success": True, "id": str(series.id)}


@router.put("/admin/series/{series_id}")
async def update_series(
    series_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Series
    data = await request.json()
    result = await db.execute(sa_select(Series).where(Series.id == uuid.UUID(series_id)))
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="Série non trouvée")
    for field in ["code", "name", "description", "matieres", "is_active"]:
        if field in data:
            setattr(series, field, data[field])
    if "exam_id" in data:
        series.exam_id = uuid.UUID(data["exam_id"]) if data["exam_id"] else None
    await db.commit()
    return {"success": True}


@router.delete("/admin/series/{series_id}")
async def delete_series(
    series_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Series
    result = await db.execute(sa_select(Series).where(Series.id == uuid.UUID(series_id)))
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="Série non trouvée")
    await db.delete(series)
    await db.commit()
    return {"success": True}


# ── CONCOURS ───────────────────────────────────────────────────────────

@router.get("/admin/concours")
async def get_concours(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Concours, ConcoursSeriesLink, Series
    result = await db.execute(
        sa_select(Concours).where(Concours.is_active == True).order_by(Concours.name)
    )
    concours_list = result.scalars().all()

    output = []
    for c in concours_list:
        series_result = await db.execute(
            sa_select(Series).join(
                ConcoursSeriesLink, ConcoursSeriesLink.series_id == Series.id
            ).where(ConcoursSeriesLink.concours_id == c.id)
        )
        series = series_result.scalars().all()
        output.append({
            "id": str(c.id), "code": c.code, "name": c.name,
            "pays": c.pays, "niveau_requis": c.niveau_requis,
            "description": c.description,
            "matieres_epreuves": c.matieres_epreuves,
            "is_active": c.is_active,
            "series_eligibles": [
                {"id": str(s.id), "code": s.code, "name": s.name} for s in series
            ],
        })
    return output


@router.post("/admin/concours")
async def create_concours(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Concours, ConcoursSeriesLink
    data = await request.json()
    c = Concours(
        code=data["code"],
        name=data["name"],
        pays=data.get("pays", "senegal"),
        niveau_requis=data.get("niveau_requis", "bac"),
        description=data.get("description"),
        matieres_epreuves=data.get("matieres_epreuves"),
        is_active=data.get("is_active", True),
    )
    db.add(c)
    await db.flush()

    for series_id in data.get("series_ids", []):
        db.add(ConcoursSeriesLink(
            concours_id=c.id,
            series_id=uuid.UUID(series_id),
        ))

    await db.commit()
    return {"success": True, "id": str(c.id)}


@router.put("/admin/concours/{concours_id}")
async def update_concours(
    concours_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Concours, ConcoursSeriesLink
    from sqlalchemy import delete as sa_delete
    data = await request.json()
    result = await db.execute(sa_select(Concours).where(Concours.id == uuid.UUID(concours_id)))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Concours non trouvé")
    for field in ["name", "pays", "niveau_requis", "description", "matieres_epreuves", "is_active"]:
        if field in data:
            setattr(c, field, data[field])
    if "series_ids" in data:
        await db.execute(
            sa_delete(ConcoursSeriesLink).where(ConcoursSeriesLink.concours_id == c.id)
        )
        for series_id in data["series_ids"]:
            db.add(ConcoursSeriesLink(
                concours_id=c.id,
                series_id=uuid.UUID(series_id),
            ))
    await db.commit()
    return {"success": True}


@router.delete("/admin/concours/{concours_id}")
async def delete_concours(
    concours_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.exam import Concours
    result = await db.execute(sa_select(Concours).where(Concours.id == uuid.UUID(concours_id)))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Concours non trouvé")
    await db.delete(c)
    await db.commit()
    return {"success": True}


# ── SIMULATIONS ───────────────────────────────────────────────────────

@router.get("/admin/simulations")
async def get_simulations(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    statut: str | None = None,
):
    from app.models.simulation import Simulation, SimulationParticipation
    query = sa_select(Simulation).order_by(Simulation.date_debut.desc())
    if statut:
        query = query.where(Simulation.statut == statut)
    result = await db.execute(query)
    simulations = result.scalars().all()

    output = []
    for s in simulations:
        # Compte les participants
        count_result = await db.scalar(
            select(func.count(SimulationParticipation.id)).where(
                SimulationParticipation.simulation_id == s.id
            )
        )
        output.append({
            "id": str(s.id),
            "titre": s.titre,
            "type": s.type,
            "exam_id": str(s.exam_id) if s.exam_id else None,
            "concours_id": str(s.concours_id) if s.concours_id else None,
            "matiere": s.matiere,
            "serie": s.serie,
            "date_debut": s.date_debut.isoformat(),
            "duree_minutes": s.duree_minutes,
            "series_eligibles": s.series_eligibles,
            "statut": s.statut,
            "notif_j1_sent": s.notif_j1_sent,
            "notif_debut_sent": s.notif_debut_sent,
            "resultats_envoyes": s.resultats_envoyes,
            "nb_participants": count_result or 0,
            "sujet_pdf_path": s.sujet_pdf_path,
            "correction_pdf_path": s.correction_pdf_path,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return output


@router.post("/admin/simulations")
async def create_simulation(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    import base64
    from pathlib import Path
    from app.models.simulation import Simulation

    data = await request.json()

    # Sauvegarde le sujet PDF si fourni en base64
    sujet_path = None
    if data.get("sujet_b64"):
        try:
            sujet_bytes = base64.b64decode(data["sujet_b64"])
            sim_dir = Path("/home/prepa/app/simulations/sujets")
            sim_dir.mkdir(parents=True, exist_ok=True)
            filename = f"sujet_{data.get('titre','sim').replace(' ','_')}_{uuid.uuid4().hex[:6]}.pdf"
            p = sim_dir / filename
            p.write_bytes(sujet_bytes)
            p.chmod(0o644)
            sujet_path = str(p)
        except Exception as e:
            print(f"Erreur sauvegarde sujet: {e}")

    # Sauvegarde la correction PDF si fournie
    correction_path = None
    if data.get("correction_b64"):
        try:
            corr_bytes = base64.b64decode(data["correction_b64"])
            corr_dir = Path("/home/prepa/app/simulations/corrections")
            corr_dir.mkdir(parents=True, exist_ok=True)
            filename = f"correction_{data.get('titre','sim').replace(' ','_')}_{uuid.uuid4().hex[:6]}.pdf"
            p = corr_dir / filename
            p.write_bytes(corr_bytes)
            p.chmod(0o644)
            correction_path = str(p)
        except Exception as e:
            print(f"Erreur sauvegarde correction: {e}")

    sim = Simulation(
        titre=data["titre"],
        type=data.get("type", "examen"),
        exam_id=uuid.UUID(data["exam_id"]) if data.get("exam_id") else None,
        concours_id=uuid.UUID(data["concours_id"]) if data.get("concours_id") else None,
        matiere=data.get("matiere"),
        serie=data.get("serie"),
        sujet_pdf_path=sujet_path,
        correction_pdf_path=correction_path,
        date_debut=datetime.fromisoformat(data["date_debut"]),
        duree_minutes=int(data.get("duree_minutes", 240)),
        series_eligibles=data.get("series_eligibles", []),
        statut="scheduled",
    )
    db.add(sim)
    await db.commit()
    return {"success": True, "id": str(sim.id), "titre": sim.titre, "statut": sim.statut}


@router.put("/admin/simulations/{simulation_id}")
async def update_simulation(
    simulation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.simulation import Simulation
    data = await request.json()
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    for field in ["titre", "type", "matiere", "serie", "duree_minutes", "series_eligibles", "statut"]:
        if field in data:
            setattr(sim, field, data[field])
    if "date_debut" in data:
        sim.date_debut = datetime.fromisoformat(data["date_debut"])
    if "exam_id" in data:
        sim.exam_id = uuid.UUID(data["exam_id"]) if data["exam_id"] else None
    if "concours_id" in data:
        sim.concours_id = uuid.UUID(data["concours_id"]) if data["concours_id"] else None
    await db.commit()
    return {"success": True}


@router.delete("/admin/simulations/{simulation_id}")
async def delete_simulation(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.simulation import Simulation
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    await db.delete(sim)
    await db.commit()
    return {"success": True}


@router.post("/admin/simulations/{simulation_id}/lancer")
async def lancer_simulation_now(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Force le lancement immédiat d'une simulation (hors scheduler)."""
    from app.models.simulation import Simulation
    from app.services.simulation_service import simulation_service
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    if sim.statut not in ("draft", "scheduled"):
        raise HTTPException(status_code=400, detail=f"Statut actuel : {sim.statut}")
    sim.statut = "scheduled"
    await db.flush()
    await simulation_service.lancer_simulation(db, sim)
    return {"success": True, "message": f"Simulation '{sim.titre}' lancée"}


@router.post("/admin/simulations/{simulation_id}/corriger")
async def corriger_simulation_now(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Force la correction immédiate de toutes les copies."""
    from app.models.simulation import Simulation
    from app.services.simulation_service import simulation_service
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    await simulation_service.corriger_toutes_copies(db, sim)
    return {"success": True, "message": f"Correction lancée pour '{sim.titre}'"}


@router.get("/admin/simulations/{simulation_id}/participants")
async def get_simulation_participants(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.simulation import SimulationParticipation
    result = await db.execute(
        sa_select(SimulationParticipation, User)
        .join(User, User.id == SimulationParticipation.user_id)
        .where(SimulationParticipation.simulation_id == uuid.UUID(simulation_id))
        .order_by(SimulationParticipation.classement.nulls_last(), SimulationParticipation.score.desc().nulls_last())
    )
    rows = result.all()
    return [
        {
            "id": str(p.id),
            "user_id": str(p.user_id),
            "user_name": u.name,
            "user_phone": u.phone_number,
            "statut": p.statut,
            "score": p.score,
            "mention": p.mention,
            "classement": p.classement,
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "corrected_at": p.corrected_at.isoformat() if p.corrected_at else None,
        }
        for p, u in rows
    ]


# ── DASHBOARD ─────────────────────────────────────────────────────────

@router.get("/admin", response_class=FileResponse)
async def admin_dashboard():
    return FileResponse("app/static/admin.html")
