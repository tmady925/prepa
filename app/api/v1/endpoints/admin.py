import uuid
from datetime import date, datetime, timezone
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

    from app.services.duplicate_detector import compute_hash, compute_content_hash, check_duplicate_document
    file_hash = compute_hash(file_bytes)
    content_hash = compute_content_hash(file_bytes)
    duplicate = await check_duplicate_document(db, file_hash, content_hash)
    if duplicate:
        type_label = "fichier identique" if duplicate["type"] == "exact" else "contenu identique"
        return {
            "success": False,
            "duplicate": True,
            "message": f"Doublon détecté ({type_label}) : {duplicate['title']}",
            "existing": duplicate,
        }

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

    # Sauvegarde les hashes sur le document créé
    if result.get("success") and result.get("document_id"):
        from app.models.document import Document
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Document)
            .where(Document.id == uuid.UUID(result["document_id"]))
            .values(file_hash=file_hash, content_hash=content_hash)
        )
        await db.commit()

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

    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Corps de requête invalide: {e}")
    file_b64 = data.get("file_b64")
    filename = data.get("filename", "document.pdf")
    exam_type = data.get("exam_type")
    series_list = data.get("series", [])
    serie = series_list[0] if series_list else data.get("serie")
    matiere = data.get("matiere")

    if not file_b64:
        raise HTTPException(status_code=400, detail="file_b64 requis")

    # Décode le fichier
    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="file_b64 invalide")

    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Fichier trop volumineux (max {_MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Fichier vide")

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

    from pathlib import Path as _P
    for ex_data in exercises_detected:
        print(f"\n  Traitement exercice {ex_data.get('number')}...")

        # Extrait et génère les PDFs
        try:
            result = await exercise_extractor_service.process_exercise(
                file_bytes=file_bytes,
                exercise_data=ex_data,
                source_filename=filename,
                matiere=analysis.get("matiere") or matiere or "autre",
                exam_type=analysis.get("exam_type") or exam_type or "bac_senegal",
                serie=analysis.get("serie") or serie,
                annee=analysis.get("annee"),
            )
        except Exception as e:
            print(f"  ⚠️ process_exercise échoué pour exercice {ex_data.get('number')}: {e}")
            result = {"exercise_path": None, "correction_path": None, "correction_generated": False, "error": str(e)}

        # Sauvegarde en DB
        effective_serie = analysis.get("serie") or serie
        effective_series = series_list if series_list else ([effective_serie] if effective_serie else [])
        effective_matiere = _normalize_matiere(analysis.get("matiere") or matiere)
        if not effective_matiere or effective_matiere not in _VALID_MATIERES:
            effective_matiere = matiere or "autre"

        ex_path_str = result.get("exercise_path")
        ex_exists = bool(ex_path_str and _P(ex_path_str).exists())

        exercise = Exercise(
            source_filename=filename,
            title=ex_data.get("title") or f"Exercice {ex_data.get('number', '')}",
            exam_type=analysis.get("exam_type") or exam_type,
            serie=effective_serie,
            series=effective_series,
            matiere=effective_matiere,
            chapitre=_sanitize_chapitre(ex_data.get("chapitre")),
            niveau=ex_data.get("niveau") if ex_data.get("niveau") in (1, 2, 3) else 2,
            annee=_validate_annee(analysis.get("annee")),
            tags=ex_data.get("tags"),
            bareme=ex_data.get("bareme"),
            exercise_number=ex_data.get("number"),
            page_debut=ex_data.get("page_debut"),
            page_fin=ex_data.get("page_fin"),
            exercise_path=ex_path_str,
            correction_path=result.get("correction_path"),
            correction_generated=result.get("correction_generated", False),
            status="ready" if ex_exists else "error",
            error_message=result.get("error") or (None if ex_exists else f"Fichier PDF absent: {ex_path_str}"),
        )
        db.add(exercise)
        saved_exercises.append(exercise)
        results.append(result)

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

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
                "number": r.get("exercise_number"),
                "title": r.get("title"),
                "exercise_path": r.get("exercise_path"),
                "correction_path": r.get("correction_path"),
                "correction_generated": r.get("correction_generated", False),
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

    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Corps de requête invalide: {e}")

    exercise_b64 = data.get("exercise_b64")
    correction_b64 = data.get("correction_b64")
    # filename_ex / filename_corr acceptés pour compatibilité mais non utilisés (noms UUID générés)
    matiere = _normalize_matiere(data.get("matiere"))
    exam_type = data.get("exam_type", "bac_senegal")
    series_list = data.get("series", [])
    serie = series_list[0] if series_list else data.get("serie")  # garde compatibilité
    chapitre = _sanitize_chapitre(data.get("chapitre"))
    annee = _validate_annee(data.get("annee"))
    tags = data.get("tags", [])
    title = data.get("title", "")

    if not exercise_b64:
        raise HTTPException(status_code=400, detail="exercise_b64 requis")
    if not matiere:
        raise HTTPException(status_code=400, detail="matiere requise")
    if matiere not in _VALID_MATIERES:
        raise HTTPException(status_code=400, detail=f"matiere invalide. Valeurs acceptées : {', '.join(sorted(_VALID_MATIERES))}")
    if exam_type and exam_type not in _VALID_EXAM_TYPES:
        raise HTTPException(status_code=400, detail=f"exam_type invalide. Valeurs acceptées : {', '.join(sorted(_VALID_EXAM_TYPES))}")

    # Décode les fichiers
    try:
        exercise_bytes = base64.b64decode(exercise_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="exercise_b64 invalide")

    if len(exercise_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Fichier trop volumineux (max {_MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if len(exercise_bytes) == 0:
        raise HTTPException(status_code=400, detail="Fichier vide")

    correction_bytes = None
    if correction_b64:
        try:
            correction_bytes = base64.b64decode(correction_b64)
            if correction_bytes == exercise_bytes:
                raise HTTPException(status_code=400, detail="La correction et l'exercice sont identiques")
        except HTTPException:
            raise
        except Exception:
            pass

    # Vérification doublon exercice (niveau 1 : fichier, niveau 2 : contenu)
    from app.services.duplicate_detector import compute_hash, compute_content_hash, check_duplicate_exercise
    exercise_hash = compute_hash(exercise_bytes)
    exercise_content_hash = compute_content_hash(exercise_bytes)
    duplicate = await check_duplicate_exercise(db, exercise_hash, exercise_content_hash)
    if duplicate:
        type_label = "fichier identique" if duplicate["type"] == "exact" else "contenu identique"
        return {
            "success": False,
            "duplicate": True,
            "message": f"Doublon détecté ({type_label}) : {duplicate['title']} — {duplicate['matiere']}",
            "existing": duplicate,
        }

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

    try:
        ex_folder = EXERCISES_DIR / matiere
        ex_folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Impossible de créer le dossier {matiere}: {e}")

    serie_clean = re.sub(r'[^A-Z0-9]', '', (serie or 'gen').upper())
    base_name = f"{exam_type}_{serie_clean}_{matiere}_{chapitre or 'general'}"
    if annee:
        base_name += f"_{annee}"
    base_name += f"_{uuid.uuid4().hex[:6]}"

    # Sauvegarde exercice
    ex_path = ex_folder / f"{base_name}.pdf"
    try:
        ex_path.write_bytes(exercise_bytes)
        ex_path.chmod(0o644)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Impossible d'écrire le fichier exercice: {e}")
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
    ex_exists = ex_path.exists()
    exercise = Exercise(
        title=title or base_name,
        exam_type=exam_type,
        serie=serie,
        series=series_list,
        matiere=matiere,
        chapitre=chapitre,
        niveau=niveau,
        annee=annee,  # déjà int via _validate_annee
        tags=tags if tags else None,
        exercise_path=str(ex_path),
        correction_path=str(corr_path) if corr_path else None,
        correction_generated=False,
        status="ready" if ex_exists else "error",
        error_message=None if ex_exists else f"Fichier PDF absent après écriture: {ex_path}",
        file_hash=exercise_hash,
        content_hash=exercise_content_hash,
    )
    db.add(exercise)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur base de données: {e}")

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


@router.post("/admin/exercises/fix-matieres")
async def fix_exercise_matieres(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Normalise les matières mal stockées en base (ex: 'chimie' → 'physique_chimie')."""
    from app.models.exercise import Exercise

    fixed = []
    skipped = []
    offset = 0
    batch_size = 200

    try:
        while True:
            result = await db.execute(
                select(Exercise).limit(batch_size).offset(offset)
            )
            exercises = result.scalars().all()
            if not exercises:
                break

            for ex in exercises:
                if not ex.matiere:
                    skipped.append({"id": str(ex.id), "raison": "matiere vide"})
                    continue
                normalized = _normalize_matiere(ex.matiere)
                if not normalized:
                    skipped.append({"id": str(ex.id), "raison": f"normalisation impossible: {ex.matiere}"})
                    continue
                if normalized != ex.matiere:
                    fixed.append({"id": str(ex.id), "avant": ex.matiere, "apres": normalized})
                    ex.matiere = normalized

            if fixed:
                await db.flush()
            offset += batch_size

        if fixed:
            await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur lors de la migration: {e}")

    return {"fixed": len(fixed), "skipped": len(skipped), "details": fixed, "skipped_details": skipped}


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

    from app.core.settings import get_settings as _get_settings
    _settings = _get_settings()
    _sim_base = Path(_settings.simulations_dir)

    # Sauvegarde le sujet PDF si fourni en base64
    sujet_path = None
    if data.get("sujet_b64"):
        try:
            sujet_bytes = base64.b64decode(data["sujet_b64"])
            sim_dir = _sim_base / "sujets"
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
            corr_dir = _sim_base / "corrections"
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
    from pathlib import Path as _Path
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    # Supprime les fichiers PDF associés
    for pdf_path in [sim.sujet_pdf_path, sim.correction_pdf_path]:
        if pdf_path:
            try:
                p = _Path(pdf_path)
                if p.exists():
                    p.unlink()
            except Exception as e:
                print(f"Erreur suppression PDF {pdf_path}: {e}")
    await db.delete(sim)
    await db.commit()
    return {"success": True}


@router.post("/admin/simulations/{simulation_id}/notifier")
async def notifier_simulation(
    simulation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """
    Envoie une notification aux utilisateurs Pro éligibles à tout moment.
    Corps JSON optionnel : { "message": "texte personnalisé" }
    """
    from app.models.simulation import Simulation
    from app.services.simulation_service import simulation_service
    result = await db.execute(sa_select(Simulation).where(Simulation.id == uuid.UUID(simulation_id)))
    sim = result.scalar_one_or_none()
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation non trouvée")
    try:
        body = await request.json()
        message_custom = body.get("message")
    except Exception:
        message_custom = None
    nb = await simulation_service.envoyer_notification_manuelle(db, sim, message_custom)
    return {"success": True, "notifies": nb, "message": f"Notification envoyée à {nb} utilisateurs Pro"}


@router.post("/admin/simulations/{simulation_id}/lancer")
async def lancer_simulation_now(
    simulation_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Lance immédiatement la simulation (envoie le sujet à tous les inscrits Pro)."""
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


# ── DASHBOARD ─────────────────────────────────────────────────────────

@router.get("/admin", response_class=FileResponse)
async def admin_dashboard():
    return FileResponse("app/static/admin.html")
