import hashlib
import hmac
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.settings import get_settings
from app.db.database import get_db
from app.services.user_service import user_service
from app.services.whatsapp.sender import whatsapp_sender
from app.services.whatsapp.messages import messages
from app.services.llm.service import call_llm
from app.repositories.message_repository import message_repo

settings = get_settings()
router = APIRouter()


@router.get("/webhook")
async def webhook_verify(request: Request):
    return {"status": "ok"}


@router.post("/webhook")
async def webhook_receive(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    data = await request.json()

    incoming = (
        data.get("messages") or
        data.get("entry", [{}])[0]
        .get("changes", [{}])[0]
        .get("value", {})
        .get("messages", [])
    )

    if not incoming:
        return {"status": "no_messages"}

    for message in incoming:
        await process_message(message, db)

    return {"status": "ok"}


def detect_complexity(text: str) -> int:
    """Détecte la complexité d'un message sur une échelle de 1 à 3."""
    text_lower = text.lower()

    complex_keywords = [
        "demontre", "prouve", "developpe", "explique en detail",
        "comment resoudre", "dissertation", "synthese", "analyse"
    ]
    medium_keywords = [
        "explique", "pourquoi", "comment", "calcule", "resous",
        "exercice", "exemple", "difference"
    ]

    if any(k in text_lower for k in complex_keywords):
        return 3
    if any(k in text_lower for k in medium_keywords):
        return 2
    return 1


async def process_message(message: dict, db: AsyncSession):
    phone = message.get("from")
    msg_type = message.get("type", "text")

    if not phone:
        return

    # Texte du message
    text = ""
    if msg_type == "text":
        text = message.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive["button_reply"].get("id", "")
        elif interactive.get("type") == "list_reply":
            text = interactive["list_reply"].get("id", "")

    if not text:
        return

    # Récupère ou crée l'élève
    user, created = await user_service.get_or_create(db, phone)

    # Vérifie le quota avant tout
    if user.status == "active":
        quota = await user_service.check_quota(user)
        if not quota["allowed"]:
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami", user.referral_code or ""),
                messages.QUOTA_BUTTONS,
            )
            return

    # Route selon l'étape d'onboarding
    await handle_onboarding(phone, text, user, db)

    # Incrémente le compteur
    await user_service.increment_message_count(db, user)


async def handle_onboarding(phone: str, text: str, user, db: AsyncSession):
    step = user.onboarding_step

    if step == "start":
        await whatsapp_sender.send_text(phone, messages.WELCOME)
        user.onboarding_step = "name"
        await db.flush()

    elif step == "name":
        user = await user_service.set_name(db, user, text)
        await whatsapp_sender.send_buttons(
            phone,
            messages.ask_exam(user.name),
            messages.EXAM_BUTTONS,
        )

    elif step == "exam":
        exam_map = {
            "exam_bac": "bac_senegal",
            "exam_bfem": "bfem",
            "exam_concours": "concours",
        }
        exam_type = exam_map.get(text, text)
        user = await user_service.set_exam(db, user, exam_type)

        if exam_type == "bac_senegal":
            await whatsapp_sender.send_list(
                phone,
                messages.ask_series_bac(user.name),
                messages.SERIES_BAC_LIST["button"],
                messages.SERIES_BAC_LIST["sections"],
            )
        else:
            user.onboarding_step = "subjects"
            await db.flush()
            await whatsapp_sender.send_text(phone, messages.ask_subjects(user.name))

    elif step == "series":
        series_map = {
            "serie_s1": "S1", "serie_s2": "S2", "serie_s3": "S3",
            "serie_l1": "L1", "serie_l2": "L2",
            "serie_t": "T", "serie_steg": "STEG",
        }
        series = series_map.get(text, text.upper())
        user = await user_service.set_series(db, user, series)
        await whatsapp_sender.send_text(phone, messages.ask_subjects(user.name))

    elif step == "subjects":
        subject_map = {
            "1": "maths", "2": "physique", "3": "svt",
            "4": "francais", "5": "philosophie",
            "6": "histoire_geo", "7": "anglais",
        }
        chosen = [
            subject_map[s.strip()]
            for s in text.split(",")
            if s.strip() in subject_map
        ]
        if not chosen:
            await whatsapp_sender.send_text(
                phone,
                "Reponds avec les numeros separes par des virgules. Ex: *1,2,4*"
            )
            return
        user = await user_service.set_subjects(db, user, chosen)
        await whatsapp_sender.send_text(phone, messages.ask_exam_date())

    elif step == "exam_date":
        try:
            exam_date = datetime.strptime(text, "%d/%m/%Y")
            user = await user_service.set_exam_date(db, user, exam_date)
            user = await user_service.complete_onboarding(db, user)
            days_left = (exam_date - datetime.now()).days
            await whatsapp_sender.send_text(
                phone,
                messages.onboarding_complete(user.name, days_left)
            )
        except ValueError:
            await whatsapp_sender.send_text(
                phone,
                "Format invalide. Utilise *JJ/MM/AAAA*\nExemple : *15/06/2026*"
            )

    elif step == "done":
        # Vérifie le quota
        quota = await user_service.check_quota(user)
        if not quota["allowed"]:
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami", user.referral_code or ""),
                messages.QUOTA_BUTTONS,
            )
            return

        # Sauvegarde le message entrant
        await message_repo.save(
            db=db,
            user_id=user.id,
            direction="inbound",
            content=text,
            intent="question_cours" if detect_complexity(text) >= 2 else "simple",
        )

        # Récupère l'historique
        history = await message_repo.get_history(db, user.id, limit=10)

        # Envoie un accusé de réception
        await whatsapp_sender.send_text(phone, "⏳ Je réfléchis...")

        # Appelle l'IA avec l'historique
        response = await call_llm(
            user_message=text,
            user_plan=user.plan,
            exam_type=user.exam_type or "",
            subject="",
            series=user.series or "",
            complexity=detect_complexity(text),
            history=history,
        )

        # Sauvegarde la réponse IA
        await message_repo.save(
            db=db,
            user_id=user.id,
            direction="outbound",
            content=response.text,
            llm_provider=response.provider,
            from_cache=response.from_cache,
        )

        # Envoie la réponse
        await whatsapp_sender.send_text(phone, response.text)

        print(f"IA ({response.provider}) -> {phone}: {response.text[:80]}...")