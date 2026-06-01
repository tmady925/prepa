import json
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.settings import get_settings
from app.db.database import get_db
from app.services.user_service import user_service, generate_referral_code
from app.services.whatsapp.sender import whatsapp_sender
from app.services.whatsapp.messages import messages
from app.services.llm.service import call_llm
from app.repositories.message_repository import message_repo
from app.services.media_processor import media_processor
from app.services.storage_service import storage_service
from app.services.rag.detector import subject_detector
from app.services.rag.mastery_service import mastery_service
from app.services.copy_analyzer_service import copy_analyzer_service
from app.db.redis import get_redis
from app.models.user import User as UserModel

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
    body = await request.body()

    # ── Vérification signature Wasender ───────────────────────────────
    if settings.whatsapp_webhook_secret:
        signature = request.headers.get("X-Webhook-Signature", "")
        if signature != settings.whatsapp_webhook_secret:
            print(f"Webhook: signature invalide")
            return {"status": "invalid_signature"}

    data = json.loads(body)

    # ── Format Wasender ───────────────────────────────────────────────
    event = data.get("event", "")

    # Ignore les événements systèmes Wasender (QR code, statut session…)
    if event in ("qrcode.updated", "session.status", "session.connected", "session.disconnected"):
        return {"status": "ignored"}

    incoming = []

    # Wasender envoie le même message via plusieurs events
    # On traite uniquement messages.received pour éviter les doublons
    if event in ("messages.received",):
        msg_data = data.get("data", {}).get("messages", {})
        if msg_data and not msg_data.get("key", {}).get("fromMe", False):
            # Détecte le type de message
            raw_message = msg_data.get("message", {}) or {}
            if "imageMessage" in raw_message:
                msg_type = "image"
            elif "documentMessage" in raw_message:
                msg_type = "document"
            else:
                msg_type = "text"

            incoming = [{
                "from": msg_data.get("key", {}).get("cleanedSenderPn", ""),
                "type": msg_type,
                "body": msg_data.get("messageBody", ""),
                "id": msg_data.get("key", {}).get("id", ""),
                "fromMe": False,
                "key": msg_data.get("key", {}),
                "message": raw_message,
            }]

    if not incoming:
        return {"status": "no_messages"}

    redis = await get_redis()

    for message in incoming:
        msg_id = message.get("id", "")
        if msg_id:
            cache_key = f"processed_msg:{msg_id}"
            already_processed = await redis.get(cache_key)
            if already_processed:
                print(f"Message {msg_id} déjà traité, ignoré")
                continue
            await redis.setex(cache_key, 3600, "1")  # 1 heure

        try:
            await process_message(message, db)
        except Exception as e:
            print(f"process_message error (phone={message.get('from', '?')}): {e}")

    return {"status": "ok"}


def detect_complexity(text: str) -> int:
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


def detect_command(text: str) -> str | None:
    commands = {
        "/aide": "aide",
        "/help": "aide",
        "aide": "aide",
        "/progression": "progression",
        "/stats": "progression",
        "progression": "progression",
        "/profil": "profil",
        "profil": "profil",
        "/inviter": "inviter",
        "/invite": "inviter",
        "inviter": "inviter",
        "/plan": "plan",
        "/pro": "plan",
        "plan": "plan",
        "action_invite": "inviter",
        "action_pro": "plan",
    }
    return commands.get(text.lower().strip())


async def _extract_exercise_text(msg_type: str, image_data: dict, message: dict) -> str:
    """Extrait le texte d'un exercice depuis une image ou un PDF."""
    import base64
    text = ""

    if msg_type == "image" and image_data:
        image_url = await copy_analyzer_service.decrypt_media(image_data)
        if image_url:
            image_bytes = await copy_analyzer_service.download_image(image_url)
            if image_bytes:
                text = await _transcribe_image_b64(base64.b64encode(image_bytes).decode())

    elif msg_type == "document":
        raw_msg = message.get("message", {}) or {}
        doc_image_data = {
            "key": message.get("key", {}),
            "message": raw_msg,
        }
        doc_url = await copy_analyzer_service.decrypt_media(doc_image_data)
        if doc_url:
            doc_bytes = await copy_analyzer_service.download_image(doc_url)
            if doc_bytes:
                try:
                    import fitz
                    doc = fitz.open(stream=doc_bytes, filetype="pdf")
                    for page in doc:
                        text += page.get_text("text")
                    doc.close()
                except Exception as e:
                    print(f"Erreur lecture PDF: {e}")

    return text.strip()


async def _transcribe_image_b64(image_b64: str) -> str:
    """Transcrit le texte d'une image via Mistral Vision."""
    from app.core.settings import get_settings
    import httpx
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": "pixtral-12b-2409",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": f"data:image/jpeg;base64,{image_b64}"
                            },
                            {
                                "type": "text",
                                "text": "Transcris exactement le texte de cet exercice scolaire. Inclus toutes les questions, données et barèmes."
                            }
                        ]
                    }],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Erreur transcription image: {e}")
    return ""


async def _do_free_correction(
    phone: str, user, db, copie_bytes: bytes, exercise_text: str
):
    """Lance la correction libre et envoie le feedback."""
    from app.services.whatsapp.sender import whatsapp_sender

    analysis = await copy_analyzer_service.analyze_copy(
        image_bytes=copie_bytes,
        exercise_text=exercise_text,
        correction_text="",
        matiere=user.subjects[0] if user.subjects else "",
        chapitre="",
        niveau=2,
        student_name=user.name or "élève",
    )

    if not analysis:
        await whatsapp_sender.send_text(
            phone,
            "❌ Impossible d'analyser ta copie. Réessaie avec une photo plus nette."
        )
    else:
        feedback = copy_analyzer_service.format_feedback(analysis, user.name or "élève")
        await whatsapp_sender.send_text(phone, feedback)
        print(f"Correction libre -> {phone}: score={analysis.get('score')}")

    user.conversation_state = {}
    await db.flush()


async def handle_command(command: str, phone: str, user, db: AsyncSession):
    if command == "aide":
        days_left = 0
        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = max(0, (exam_date - datetime.now()).days)
        await whatsapp_sender.send_text(
            phone,
            messages.help_message(user.name or "ami", days_left)
        )

    elif command == "progression":
        await whatsapp_sender.send_text(
            phone,
            messages.progression_message(user)
        )

    elif command == "profil":
        profile = await mastery_service.get_student_profile(db, user.id)
        if not profile:
            await whatsapp_sender.send_text(
                phone,
                f"📊 *Ton profil, {user.name}*\n\n"
                "Tu n'as pas encore travaillé de chapitres.\n\n"
                "Pose une question de cours pour commencer ! 🚀"
            )
        else:
            msg = f"📊 *Carte du savoir, {user.name}*\n\n"
            for matiere, chapitres in profile.items():
                msg += f"*{matiere.upper()}*\n"
                for chapitre, data in chapitres.items():
                    level = data["level"]
                    if level < 0.3:
                        emoji = "🔴"
                        label = "À renforcer"
                    elif level < 0.6:
                        emoji = "🟡"
                        label = "En cours"
                    else:
                        emoji = "🟢"
                        label = "Maîtrisé"
                    chapitre_label = chapitre.replace("_", " ").title()
                    msg += f"  {emoji} {chapitre_label} — {label} ({int(level*100)}%)\n"
                    if data.get("weak_points"):
                        msg += f"     ⚠️ {', '.join(data['weak_points'][:2])}\n"
                msg += "\n"
            msg += "Tape */aide* pour voir les commandes disponibles."
            await whatsapp_sender.send_text(phone, msg)

    elif command == "inviter":
        if not user.referral_code:
            user.referral_code = generate_referral_code(user.name or "")
            await db.flush()
        await whatsapp_sender.send_text(
            phone,
            messages.invite_message(user)
        )

    elif command == "plan":
        if user.plan == "pro":
            await whatsapp_sender.send_text(phone, messages.plan_message(user))
        else:
            from app.services.payment_service import payment_service
            invoice = await payment_service.create_invoice(user=user, plan="pro")
            if invoice.get("success"):
                await whatsapp_sender.send_text(
                    phone,
                    f"💳 *Passe Prepa Pro maintenant !*\n\n"
                    f"✅ Messages illimités\n"
                    f"✅ Corrections détaillées\n\n"
                    f"💰 *3000 FCFA/mois*\n\n"
                    f"👉 Clique ici pour payer :\n{invoice['payment_url']}\n\n"
                    f"_Paiement sécurisé via Wave, Orange Money ou Free Money 🔒_"
                )
            else:
                await whatsapp_sender.send_text(
                    phone,
                    "❌ Impossible de créer le lien de paiement. Réessaie dans quelques instants."
                )


async def process_message(message: dict, db: AsyncSession):
    phone = message.get("from")
    msg_type = message.get("type", "text")

    if not phone:
        return

    image_data = None
    text = ""
    if msg_type == "image":
        image_data = {
            "key": message.get("key", {}),
            "message": message.get("message", {}),
        }
    elif msg_type == "text":
        text = message.get("body", "").strip()
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive["button_reply"].get("id", "")
        elif interactive.get("type") == "list_reply":
            text = interactive["list_reply"].get("id", "")
    elif msg_type == "button":
        text = message.get("button", {}).get("payload", "") or message.get("body", "")

    if not text and not image_data:
        return

    user, created = await user_service.get_or_create(db, phone)

    # Détecte un code de parrainage dans le premier message
    if created and text.upper().startswith("PREPA-"):
        code = text.strip().upper().replace("PREPA-", "")
        applied = await user_service.apply_referral(db, user, code)
        if applied:
            print(f"Parrainage auto détecté: {code}")
            if user.referred_by_id:
                result = await db.execute(
                    sa_select(UserModel).where(UserModel.id == user.referred_by_id)
                )
                referrer = result.scalar_one_or_none()
                if referrer:
                    await whatsapp_sender.send_text(
                        referrer.phone_number,
                        f"🎉 Un ami vient de s'inscrire avec ton lien !\n\n"
                        f"Tu gagneras *20 messages bonus* quand il sera actif 💪"
                    )

    if user.status == "active":
        quota = await user_service.check_quota(user)
        if not quota["allowed"]:
            # Détecte toutes les variantes possibles
            text_lower = text.lower().strip()
            if text_lower in ("action_invite", "1", "inviter des amis", "inviter", "/inviter"):
                await handle_command("inviter", phone, user, db)
                return
            if text_lower in ("action_pro", "2", "passer pro", "pro", "/plan"):
                await handle_command("plan", phone, user, db)
                return
            # Affiche le message quota avec options
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami"),
                messages.QUOTA_BUTTONS,
            )
            return

    await handle_onboarding(phone, text, user, db, msg_type=msg_type, image_data=image_data, message=message)
    await user_service.increment_message_count(db, user)


async def _ask_exam(phone: str, user, db: AsyncSession):
    """Charge les examens depuis DB et les envoie à l'élève."""
    from app.models.exam import Exam

    query = sa_select(Exam).where(Exam.is_active == True)
    if user.pays:
        query = query.where(Exam.pays == user.pays)
    query = query.order_by(Exam.niveau, Exam.name)

    result = await db.execute(query)
    exams = result.scalars().all()

    if not exams:
        # Fallback — charge tous les examens actifs
        result2 = await db.execute(
            sa_select(Exam).where(Exam.is_active == True).order_by(Exam.name)
        )
        exams = result2.scalars().all()

    exams_data = [
        {"code": e.code, "name": e.name, "pays": e.pays}
        for e in exams
    ]

    user.onboarding_step = "exam"
    await db.flush()

    if len(exams_data) <= 3:
        buttons = messages.build_exam_buttons(exams_data)
        await whatsapp_sender.send_buttons(
            phone,
            messages.ask_exam_dynamic(user.name, exams_data),
            buttons,
        )
    else:
        list_config = messages.build_exam_list(exams_data)
        await whatsapp_sender.send_list(
            phone,
            messages.ask_exam_dynamic(user.name, exams_data),
            list_config["button"],
            list_config["sections"],
        )


async def handle_onboarding(phone: str, text: str, user, db: AsyncSession, msg_type: str = "text", image_data: dict = None, message: dict = None):
    step = user.onboarding_step

    if step == "start":
        await whatsapp_sender.send_text(phone, messages.WELCOME)
        user.onboarding_step = "name"
        await db.flush()

    elif step == "name":
        user = await user_service.set_name(db, user, text)

        # Détecte le pays depuis l'indicatif
        from app.services.phone_detector import detect_pays
        pays_info = detect_pays(phone)

        if pays_info:
            user.conversation_state = {
                "detected_pays": pays_info["pays"],
                "detected_pays_nom": pays_info["nom"],
                "detected_pays_flag": pays_info["flag"],
            }
            await db.flush()
            await whatsapp_sender.send_buttons(
                phone,
                messages.ask_confirm_pays(user.name, pays_info["nom"], pays_info["flag"]),
                messages.CONFIRM_PAYS_BUTTONS,
            )
            user.onboarding_step = "confirm_pays"
            await db.flush()
        else:
            # Pays non détecté → demande manuel
            await whatsapp_sender.send_text(phone, messages.ask_pays_manuel())
            user.onboarding_step = "saisie_pays"
            await db.flush()

    elif step == "confirm_pays":
        from app.services.choice_detector import detect_choice

        choices = [
            {"id": "pays_oui", "title": "Oui", "value": "oui"},
            {"id": "pays_non", "title": "Non", "value": "non"},
        ]
        choice = detect_choice(text, choices)

        if choice and choice["value"] == "oui":
            conv_state = user.conversation_state or {}
            user.pays = conv_state.get("detected_pays")
            user.conversation_state = {}
            await db.flush()
            await _ask_exam(phone, user, db)
        elif choice and choice["value"] == "non":
            await whatsapp_sender.send_text(phone, messages.ask_pays_manuel())
            user.onboarding_step = "saisie_pays"
            user.conversation_state = {}
            await db.flush()
        else:
            conv_state = user.conversation_state or {}
            await whatsapp_sender.send_buttons(
                phone,
                messages.ask_confirm_pays(
                    user.name,
                    conv_state.get("detected_pays_nom", ""),
                    conv_state.get("detected_pays_flag", ""),
                ),
                messages.CONFIRM_PAYS_BUTTONS,
            )

    elif step == "saisie_pays":
        from app.services.phone_detector import PAYS_MAP
        pays_trouve = None
        text_lower = text.lower().strip()

        for data in PAYS_MAP.values():
            if (data["nom"].lower() in text_lower or
                    data["pays"].replace("_", " ") in text_lower or
                    text_lower in data["nom"].lower()):
                pays_trouve = data
                break

        if pays_trouve:
            user.pays = pays_trouve["pays"]
            await db.flush()
            await whatsapp_sender.send_text(
                phone,
                f"Super ! {pays_trouve['flag']} *{pays_trouve['nom']}* noté !\n"
            )
        else:
            # Pays non reconnu → met senegal par défaut
            user.pays = "senegal"
            await db.flush()

        await _ask_exam(phone, user, db)

    elif step == "exam":
        from app.models.exam import Exam, Series
        from app.services.choice_detector import detect_choice

        # Charge les examens disponibles (même filtre que _ask_exam)
        query = sa_select(Exam).where(Exam.is_active == True)
        if user.pays:
            query = query.where(Exam.pays == user.pays)
        query = query.order_by(Exam.niveau, Exam.name)
        result = await db.execute(query)
        all_exams = result.scalars().all()

        # Construit la liste de choix
        choices = [
            {
                "id": f"exam_{e.code}",
                "title": e.name,
                "value": e.code,
                "obj": e,
            }
            for e in all_exams
        ]

        choice = detect_choice(text, choices)

        if not choice:
            await _ask_exam(phone, user, db)
            return

        exam = choice["obj"]
        user = await user_service.set_exam(db, user, exam.code)

        # Charge les séries de cet examen depuis DB
        series_result = await db.execute(
            sa_select(Series).where(
                Series.exam_id == exam.id,
                Series.is_active == True,
            ).order_by(Series.code)
        )
        series_list = series_result.scalars().all()

        if series_list:
            series_data = [
                {"code": s.code, "name": s.name, "description": s.description or ""}
                for s in series_list
            ]
            series_list_config = messages.build_series_list(series_data, exam.name)
            await whatsapp_sender.send_list(
                phone,
                messages.ask_series_bac(user.name),
                series_list_config["button"],
                series_list_config["sections"],
            )
        else:
            # Pas de série pour cet examen → passe aux matières
            user.onboarding_step = "subjects"
            await db.flush()
            await whatsapp_sender.send_text(phone, messages.ask_subjects(user.name))

    elif step == "series":
        from app.models.exam import Series, Exam
        from app.services.choice_detector import detect_choice

        # Charge les séries de l'examen de l'élève
        exam_result = await db.execute(
            sa_select(Exam).where(Exam.code == user.exam_type)
        )
        exam = exam_result.scalar_one_or_none()

        all_series = []
        if exam:
            series_result = await db.execute(
                sa_select(Series).where(
                    Series.exam_id == exam.id,
                    Series.is_active == True,
                ).order_by(Series.code)
            )
            all_series = series_result.scalars().all()

        choices = [
            {
                "id": f"serie_{s.code.lower()}",
                "title": s.code,
                "value": s.code,
                "obj": s,
            }
            for s in all_series
        ]

        choice = detect_choice(text, choices)

        if not choice and all_series:
            # Relance la liste
            series_data = [
                {"code": s.code, "name": s.name, "description": s.description or ""}
                for s in all_series
            ]
            series_list_config = messages.build_series_list(series_data, exam.name if exam else "")
            await whatsapp_sender.send_list(
                phone,
                "Je n'ai pas compris 😅 Choisis ta série :",
                series_list_config["button"],
                series_list_config["sections"],
            )
            return

        series_code = choice["value"] if choice else text.upper()
        user = await user_service.set_series(db, user, series_code)
        await whatsapp_sender.send_text(phone, messages.ask_subjects(user.name))

    elif step == "subjects":
        subject_map = {
            "1": "maths", "2": "physique_chimie", "3": "svt",
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
                "Réponds avec les numéros séparés par des virgules. Ex: *1,2,4*"
            )
            return
        user = await user_service.set_subjects(db, user, chosen)
        await whatsapp_sender.send_text(phone, messages.ask_exam_date())

    elif step == "exam_date":
        try:
            exam_date = datetime.strptime(text, "%d/%m/%Y")
            user = await user_service.set_exam_date(db, user, exam_date)
            await whatsapp_sender.send_buttons(
                phone,
                messages.ask_plan(user.name),
                messages.PLAN_ONBOARDING_BUTTONS,
            )
        except ValueError:
            await whatsapp_sender.send_text(
                phone,
                "Format invalide. Utilise *JJ/MM/AAAA*\nExemple : *15/06/2026*"
            )

    elif step == "plan":
        user = await user_service.complete_onboarding(db, user)
        days_left = 0
        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = max(0, (exam_date - datetime.now()).days)
        await whatsapp_sender.send_text(
            phone,
            messages.onboarding_complete(user.name, days_left)
        )
        if text in ("onboarding_pro", "action_pro"):
            from app.services.payment_service import payment_service
            invoice = await payment_service.create_invoice(user=user, plan="pro")
            if invoice.get("success"):
                await whatsapp_sender.send_text(
                    phone,
                    f"💳 Voici ton lien de paiement :\n\n{invoice['payment_url']}\n\n"
                    "Paiement sécurisé via Wave, Orange Money ou Free Money 🔒"
                )

    elif step == "done":
        quota = await user_service.check_quota(user)
        if not quota["allowed"]:
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami"),
                messages.QUOTA_BUTTONS,
            )
            return

        # Traitement copie simulation (priorité sur copie exercice)
        if msg_type == "image" and image_data:
            conv_state = user.conversation_state or {}
            if conv_state.get("awaiting_simulation_copy"):
                from app.services.simulation_service import simulation_service
                import uuid as uuid_module

                sim_id = conv_state.get("simulation_id")
                heure_fin_str = conv_state.get("heure_fin", "")
                simulation_titre = conv_state.get("simulation_titre", "")

                await whatsapp_sender.send_text(phone, "📸 Copie reçue ! Analyse en cours... ⏳")

                # Décrypte l'image
                image_url = await copy_analyzer_service.decrypt_media(image_data)
                if not image_url:
                    await whatsapp_sender.send_text(phone, "❌ Impossible de lire l'image. Réessaie.")
                    return

                image_bytes = await copy_analyzer_service.download_image(image_url)
                if not image_bytes:
                    await whatsapp_sender.send_text(phone, "❌ Erreur téléchargement. Réessaie.")
                    return

                # Soumet la copie
                result = await simulation_service.soumettre_copie(
                    db=db,
                    simulation_id=uuid_module.UUID(sim_id),
                    user_id=user.id,
                    image_bytes=image_bytes,
                )

                if result.get("success"):
                    await whatsapp_sender.send_text(
                        phone,
                        f"✅ Ta copie pour *{simulation_titre}* a été soumise !\n\n"
                        f"Les résultats seront envoyés après la correction de toutes les copies. 📊\n\n"
                        f"_Tu seras notifié(e) dès que ton score est prêt._"
                    )
                    # Réinitialise l'état
                    user.conversation_state = {}
                    await db.flush()
                else:
                    error = result.get("error", "Erreur inconnue")
                    if "Délai dépassé" in error:
                        await whatsapp_sender.send_text(
                            phone,
                            f"⏰ Désolé, le délai de soumission est dépassé.\n\n"
                            f"La correction sera quand même envoyée à tous les participants. 📄"
                        )
                        user.conversation_state = {}
                        await db.flush()
                    else:
                        await whatsapp_sender.send_text(phone, f"❌ {error}")

                print(f"Simulation copie soumise -> {phone}: sim={sim_id}")
                return

        # Traitement copie manuscrite
        if msg_type == "image" and image_data:
            conv_state = user.conversation_state or {}
            if conv_state.get("awaiting_copy"):
                from app.models.exercise import Exercise
                from sqlalchemy import select
                import uuid as uuid_module

                await whatsapp_sender.send_text(phone, "📸 Je reçois ta copie... ⏳ Analyse en cours...")

                # Décrypte l'image
                image_url = await copy_analyzer_service.decrypt_media(image_data)
                if not image_url:
                    await whatsapp_sender.send_text(phone, "❌ Impossible de lire l'image. Réessaie avec une meilleure photo.")
                    return

                # Télécharge l'image
                image_bytes = await copy_analyzer_service.download_image(image_url)
                if not image_bytes:
                    await whatsapp_sender.send_text(phone, "❌ Erreur téléchargement image. Réessaie.")
                    return

                # Récupère l'exercice en DB
                exercise_id = conv_state.get("exercise_id")
                exercise_db = None
                correction_text = ""
                exercise_text = ""

                if exercise_id:
                    result_ex = await db.execute(
                        select(Exercise).where(Exercise.id == uuid_module.UUID(exercise_id))
                    )
                    exercise_db = result_ex.scalar_one_or_none()

                if exercise_db:
                    # Extrait le texte de l'exercice
                    try:
                        import fitz
                        from pathlib import Path
                        ex_path = Path(exercise_db.exercise_path)
                        if ex_path.exists():
                            doc = fitz.open(str(ex_path))
                            for page in doc:
                                exercise_text += page.get_text("text")
                            doc.close()
                    except Exception:
                        pass

                    # Extrait le texte de la correction si disponible
                    if exercise_db.correction_path:
                        try:
                            corr_path = Path(exercise_db.correction_path)
                            if corr_path.exists():
                                doc = fitz.open(str(corr_path))
                                for page in doc:
                                    correction_text += page.get_text("text")
                                doc.close()
                        except Exception:
                            pass

                # Analyse la copie
                print(f"  → Exercise text ({len(exercise_text)} chars): {exercise_text[:200]}")
                print(f"  → Correction text ({len(correction_text)} chars): {correction_text[:200]}")
                analysis = await copy_analyzer_service.analyze_copy(
                    image_bytes=image_bytes,
                    exercise_text=exercise_text,
                    correction_text=correction_text,
                    matiere=conv_state.get("matiere", ""),
                    chapitre=conv_state.get("chapitre", ""),
                    niveau=exercise_db.niveau if exercise_db else 2,
                    student_name=user.name or "élève",
                )

                if not analysis:
                    await whatsapp_sender.send_text(
                        phone,
                        "❌ Impossible d'analyser ta copie. Réessaie avec une photo plus nette."
                    )
                    return

                # Envoie le feedback
                feedback = copy_analyzer_service.format_feedback(analysis, user.name or "élève")
                await whatsapp_sender.send_text(phone, feedback)

                # Envoie la correction PDF si disponible
                if exercise_db and exercise_db.correction_path:
                    from pathlib import Path
                    corr_path = Path(exercise_db.correction_path)
                    if corr_path.exists():
                        corr_url = f"http://72.62.4.97/corrections/{exercise_db.matiere}/{corr_path.name}"
                        payload = {
                            "to": phone,
                            "documentUrl": corr_url,
                            "fileName": corr_path.name,
                            "text": "📄 Voici la correction complète",
                        }
                        await whatsapp_sender._send(payload)

                # Met à jour la maîtrise du chapitre selon le score de la copie
                score_copie = analysis.get("score", 0)
                if conv_state.get("chapitre"):
                    try:
                        await mastery_service.update_after_interaction(
                            db=db,
                            user_id=user.id,
                            matiere=conv_state.get("matiere", ""),
                            chapitre=conv_state.get("chapitre", ""),
                            detection={"confiance": 0.9},
                            response_text=f"Score copie: {score_copie}/100",
                        )
                    except Exception as e:
                        print(f"  → Mastery update error: {e}")

                # Réinitialise l'état
                user.conversation_state = {}
                await db.flush()

                print(f"Copie analysée -> {phone}: score={score_copie}")
                return
            else:
                # Image reçue sans exercice en cours
                # Détecte l'intention via le caption
                raw_msg = message.get("message", {}) or {}
                caption = ""
                if msg_type == "image":
                    caption = (raw_msg.get("imageMessage", {}).get("caption", "") or "").lower()
                elif msg_type == "document":
                    caption = (raw_msg.get("documentMessage", {}).get("caption", "") or "").lower()

                exercise_indicators = [
                    "exercice", "enoncé", "enonce", "sujet", "question",
                    "corrige", "voici", "l'exercice", "probleme", "problème",
                ]
                copy_indicators = [
                    "copie", "essai", "essaie", "réponse", "reponse",
                    "mon travail", "ma réponse", "j'ai fait",
                ]

                is_exercise = any(kw in caption for kw in exercise_indicators)
                is_copy = any(kw in caption for kw in copy_indicators)

                if is_exercise:
                    # Élève envoie l'exercice en premier
                    await whatsapp_sender.send_text(phone, "⏳ Je lis l'exercice...")
                    exercise_text = await _extract_exercise_text(msg_type, image_data, message)
                    if not exercise_text:
                        await whatsapp_sender.send_text(
                            phone,
                            "❌ Je n'ai pas pu lire l'exercice.\n"
                            "Envoie une photo plus nette ou un PDF."
                        )
                        return
                    user.conversation_state = {
                        "awaiting_copy_for_free_correction": True,
                        "exercise_text": exercise_text,
                    }
                    await db.flush()
                    await whatsapp_sender.send_text(
                        phone,
                        "📄 Exercice reçu ✅\n\n"
                        "Maintenant envoie-moi *ta copie* 📸\n"
                        "_Photo claire de ta feuille de réponse_"
                    )
                    return

                elif is_copy:
                    # Élève envoie sa copie en premier
                    image_url = await copy_analyzer_service.decrypt_media(image_data)
                    if not image_url:
                        await whatsapp_sender.send_text(phone, "❌ Impossible de lire l'image.")
                        return
                    image_bytes = await copy_analyzer_service.download_image(image_url)
                    if not image_bytes:
                        await whatsapp_sender.send_text(phone, "❌ Erreur téléchargement.")
                        return
                    import base64
                    user.conversation_state = {
                        "awaiting_exercise_for_correction": True,
                        "copie_b64": base64.b64encode(image_bytes).decode(),
                    }
                    await db.flush()
                    await whatsapp_sender.send_text(
                        phone,
                        "📸 Copie reçue ✅\n\n"
                        "Maintenant envoie-moi *l'exercice* 📄\n\n"
                        "- Une photo de l'énoncé 📸\n"
                        "- Ou un PDF 📎"
                    )
                    return

                else:
                    # Analyse l'image pour déterminer si c'est un exercice ou une copie
                    await whatsapp_sender.send_text(phone, "⏳ J'analyse ton image...")

                    image_url = await copy_analyzer_service.decrypt_media(image_data)
                    if not image_url:
                        await whatsapp_sender.send_text(phone, "❌ Impossible de lire l'image.")
                        return
                    image_bytes = await copy_analyzer_service.download_image(image_url)
                    if not image_bytes:
                        await whatsapp_sender.send_text(phone, "❌ Erreur téléchargement.")
                        return

                    import base64, httpx
                    image_b64 = base64.b64encode(image_bytes).decode()

                    # Mistral Vision détecte le type d'image
                    image_type = "copie"  # défaut
                    try:
                        from app.core.settings import get_settings as _get_settings
                        _settings = _get_settings()
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            resp = await client.post(
                                "https://api.mistral.ai/v1/chat/completions",
                                headers={"Authorization": f"Bearer {_settings.mistral_api_key}"},
                                json={
                                    "model": "pixtral-12b-2409",
                                    "messages": [{
                                        "role": "user",
                                        "content": [
                                            {"type": "image_url", "image_url": f"data:image/jpeg;base64,{image_b64}"},
                                            {"type": "text", "text": "Cette image est-elle : (A) un énoncé d'exercice/sujet scolaire avec des questions, ou (B) une copie d'élève avec des réponses manuscrites ? Réponds uniquement par A ou B."}
                                        ]
                                    }],
                                    "temperature": 0.1,
                                    "max_tokens": 5,
                                }
                            )
                            data = resp.json()
                            if "choices" in data:
                                answer = data["choices"][0]["message"]["content"].strip().upper()
                                if "A" in answer:
                                    image_type = "exercice"
                    except Exception as e:
                        print(f"Erreur détection type image: {e}")

                    if image_type == "exercice":
                        # C'est un exercice → transcrit et attend la copie
                        exercise_text = await _transcribe_image_b64(image_b64)
                        if not exercise_text:
                            await whatsapp_sender.send_text(
                                phone,
                                "❌ Je n'ai pas pu lire l'exercice. Envoie une photo plus nette."
                            )
                            return
                        user.conversation_state = {
                            "awaiting_copy_for_free_correction": True,
                            "exercise_text": exercise_text,
                        }
                        await db.flush()
                        await whatsapp_sender.send_text(
                            phone,
                            "📄 Exercice reçu ✅\n\n"
                            "Maintenant envoie-moi *ta copie* 📸\n"
                            "_Photo claire de ta feuille de réponse_"
                        )
                    else:
                        # C'est une copie → stocke et attend l'exercice
                        user.conversation_state = {
                            "awaiting_exercise_for_correction": True,
                            "copie_b64": image_b64,
                        }
                        await db.flush()
                        await whatsapp_sender.send_text(
                            phone,
                            "📸 Copie reçue ✅\n\n"
                            "Maintenant envoie-moi *l'exercice* 📄\n\n"
                            "- Une photo de l'énoncé 📸\n"
                            "- Ou un PDF 📎"
                        )
                    return

        # Élève a un exercice en cours (awaiting_copy)
        conv_state = user.conversation_state or {}
        if conv_state.get("awaiting_copy"):
            # Détecte si l'élève demande directement la correction sans photo
            correction_keywords = ["correction", "corrige", "corrigé", "solution", "réponse", "reponse"]
            if any(kw in text.lower() for kw in correction_keywords):
                await whatsapp_sender.send_text(
                    phone,
                    "📸 Pour recevoir la correction, envoie d'abord une photo de ta copie !\n\n"
                    "Je vais analyser ton travail avant de te donner la correction. 💪\n\n"
                    "_Si tu veux abandonner cet exercice, tape_ *skip*"
                )
                return

            # Élève veut passer l'exercice
            if text.lower().strip() in ("skip", "passer", "/skip"):
                user.conversation_state = {}
                await db.flush()
                await whatsapp_sender.send_text(
                    phone,
                    "✅ Exercice annulé. Pose-moi une nouvelle question ou demande un autre exercice !"
                )
                return

            # Tout autre texte pendant awaiting_copy → rappel
            if msg_type != "image":
                await whatsapp_sender.send_text(
                    phone,
                    "📸 Je t'attends ! Envoie une photo de ta copie pour que je te corrige.\n\n"
                    "_Tape_ *skip* _pour annuler l'exercice._"
                )
                return

        # ── Correction libre ─────────────────────────────────────────
        conv_state = user.conversation_state or {}

        # Bloc 2 — Bot a la copie, attend l'exercice
        if conv_state.get("awaiting_exercise_for_correction"):
            if msg_type not in ("image", "document"):
                await whatsapp_sender.send_text(
                    phone,
                    "📄 Envoie une *photo* ou un *PDF* de l'exercice."
                )
                return

            await whatsapp_sender.send_text(phone, "⏳ J'analyse l'exercice et ta copie...")
            exercise_text = await _extract_exercise_text(msg_type, image_data, message)

            if not exercise_text:
                await whatsapp_sender.send_text(
                    phone,
                    "❌ Je n'ai pas pu lire l'exercice. Envoie une photo plus nette."
                )
                return

            import base64
            copie_b64 = conv_state.get("copie_b64", "")
            if not copie_b64:
                await whatsapp_sender.send_text(phone, "❌ Ta copie a expiré. Renvoie-la.")
                user.conversation_state = {}
                await db.flush()
                return

            copie_bytes = base64.b64decode(copie_b64)
            await _do_free_correction(phone, user, db, copie_bytes, exercise_text)
            return

        # Bloc 3 — Bot a l'exercice, attend la copie
        if conv_state.get("awaiting_copy_for_free_correction"):
            if msg_type != "image":
                await whatsapp_sender.send_text(
                    phone,
                    "📸 Envoie une *photo* de ta copie."
                )
                return

            await whatsapp_sender.send_text(phone, "⏳ J'analyse ta copie...")
            image_url = await copy_analyzer_service.decrypt_media(image_data)
            if not image_url:
                await whatsapp_sender.send_text(phone, "❌ Impossible de lire l'image.")
                return
            image_bytes = await copy_analyzer_service.download_image(image_url)
            if not image_bytes:
                await whatsapp_sender.send_text(phone, "❌ Erreur téléchargement.")
                return

            exercise_text = conv_state.get("exercise_text", "")
            await _do_free_correction(phone, user, db, image_bytes, exercise_text)
            return

        # Détecte les commandes spéciales
        command = detect_command(text)
        if command:
            await handle_command(command, phone, user, db)
            await user_service.increment_message_count(db, user)
            return

        # Sauvegarde le message entrant
        await message_repo.save(
            db=db,
            user_id=user.id,
            direction="inbound",
            content=text,
            intent="question_cours" if detect_complexity(text) >= 2 else "simple",
        )

        history = await message_repo.get_history(db, user.id, limit=10)

        await whatsapp_sender.send_text(phone, "⏳ Je réfléchis...")

        # Détecte matière et chapitre automatiquement
        detection = await subject_detector.detect(
            text,
            {
                "exam_type": user.exam_type,
                "serie": user.series,
                "matiere": user.subjects[0] if user.subjects else None,
            }
        )
        detected_matiere = detection.get("matiere") or ""
        detected_chapitre = detection.get("chapitre") or ""

        # Fallback matière
        if not detected_matiere and user.subjects:
            detected_matiere = user.subjects[0]
            print(f"Fallback matière: {detected_matiere}")

        detection["user_id"] = str(user.id)
        print(f"Détection: {detected_matiere}/{detected_chapitre} ({detection.get('confiance')})")

        # ── Mode exercice ─────────────────────────────────────────────
        conv_state = user.conversation_state or {}

        # L'élève répond à un exercice en cours
        if conv_state.get("awaiting_answer"):
            from app.services.rag.exercise_generator import exercise_generator

            analysis = await exercise_generator.analyze_answer(
                db=db,
                user_id=user.id,
                matiere=conv_state.get("matiere", detected_matiere),
                chapitre=conv_state.get("chapitre", detected_chapitre),
                exercise_text=conv_state.get("exercise_text", ""),
                student_answer=text,
                exercise_type=conv_state.get("exercise_type", ""),
                hints_asked=conv_state.get("hints_asked", 0),
            )

            score = analysis.get("score", 0)
            correct = analysis.get("correct", False)
            encouragement = analysis.get("encouragement", "Continue ! 💪")
            correction = analysis.get("correction", "")
            points_forts = analysis.get("points_forts", [])
            erreurs = analysis.get("erreurs", [])
            prochain = analysis.get("prochain_conseil", "")

            score_emoji = "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"

            response_text = f"{score_emoji} *Score : {score}/100*\n\n"

            if points_forts:
                response_text += "*Points forts :*\n"
                for p in points_forts[:2]:
                    response_text += f"- {p}\n"
                response_text += "\n"

            if erreurs:
                response_text += "*Points à corriger :*\n"
                for e in erreurs[:2]:
                    response_text += f"- {e}\n"
                response_text += "\n"

            response_text += f"*Correction :*\n{correction}\n\n"
            response_text += encouragement

            if prochain:
                response_text += f"\n\n💡 *Conseil :* {prochain}"

            # Réinitialise l'état
            user.conversation_state = {}
            await db.flush()

            await message_repo.save(
                db=db,
                user_id=user.id,
                direction="outbound",
                content=response_text,
                llm_provider="exercise_analyzer",
                from_cache=False,
            )

            blocks = await media_processor.process(response_text)
            for block in blocks:
                if block["type"] == "text":
                    await whatsapp_sender.send_text(phone, block["content"])
                elif block["type"] == "image":
                    url = await storage_service.upload_image(block["content"])
                    if url:
                        await whatsapp_sender.send_image_url(phone, url)

            print(f"Correction exercice -> {phone}: score={score}")
            return

        # L'élève demande un indice pendant un exercice
        if conv_state.get("exercise_text") and any(
            kw in text.lower() for kw in [
                "indice", "aide", "hint", "help",
                "je sais pas", "je ne sais pas", "bloque", "bloqué"
            ]
        ):
            conv_state["hints_asked"] = conv_state.get("hints_asked", 0) + 1
            user.conversation_state = conv_state
            await db.flush()

            hint_prompt = (
                f"L'élève demande un indice pour cet exercice :\n"
                f"{conv_state.get('exercise_text', '')}\n\n"
                f"Donne UN seul indice utile sans donner la réponse. "
                f"Sois encourageant et concis."
            )
            response = await call_llm(
                user_message=hint_prompt,
                user_plan=user.plan,
                exam_type=user.exam_type or "",
                subject=conv_state.get("matiere", ""),
                series=user.series or "",
                complexity=1,
                history=[],
                db=None,
            )
            await whatsapp_sender.send_text(
                phone,
                f"💡 *Indice {conv_state['hints_asked']} :*\n\n{response.text}"
            )
            return

        # Demande d'exercice → cherche d'abord un PDF en DB, sinon génère
        exercise_keywords = [
            "exercice", "exercices", "entraîner", "entrainer",
            "donne moi un exercice", "série", "annale", "pratiquer",
            "donne un exercice", "fais moi un exercice"
        ]
        wants_exercise = (
            detection.get("type_demande") == "exercice"
            or any(kw in text.lower() for kw in exercise_keywords)
        )

        if wants_exercise and detected_matiere:
            from sqlalchemy import select, func
            from app.models.exercise import Exercise

            # ── Détermine le niveau adapté au profil élève ────────────
            niveau_cible = 2  # défaut intermédiaire

            score = getattr(user, "engagement_score", 50) or 50
            if score < 30:
                niveau_cible = 1
            elif score > 70:
                niveau_cible = 3

            # Affine avec la maîtrise du chapitre si disponible
            if detected_chapitre:
                try:
                    profile = await mastery_service.get_student_profile(db, user.id)
                    if profile and detected_matiere in profile:
                        chapitre_data = profile[detected_matiere].get(detected_chapitre, {})
                        maitrise = chapitre_data.get("level", 0.5)
                        if maitrise < 0.3:
                            niveau_cible = 1
                        elif maitrise > 0.6:
                            niveau_cible = min(3, niveau_cible + 1)
                except Exception:
                    pass

            print(f"  → Niveau cible pour {user.name}: {niveau_cible} (score={score})")

            # ── Cherche un exercice du bon niveau ─────────────────────
            def _base_query():
                return select(Exercise).where(
                    Exercise.status == "ready",
                    Exercise.exercise_path.isnot(None),
                    Exercise.matiere == detected_matiere,
                )

            def _add_filters(q, niveau=None):
                if niveau is not None:
                    q = q.where(Exercise.niveau == niveau)
                if user.exam_type:
                    q = q.where(Exercise.exam_type == user.exam_type)
                if user.series:
                    q = q.where(Exercise.serie == user.series)
                if detected_chapitre:
                    q = q.where(Exercise.chapitre == detected_chapitre)
                return q.order_by(func.random()).limit(1)

            result = await db.execute(_add_filters(_base_query(), niveau_cible))
            exercise_db = result.scalar_one_or_none()

            # Fallback niveau ±1 puis sans filtre niveau/chapitre
            if not exercise_db:
                for niveau_fallback in [niveau_cible - 1, niveau_cible + 1, None]:
                    if niveau_fallback is not None and niveau_fallback not in (1, 2, 3):
                        continue
                    r = await db.execute(_add_filters(_base_query(), niveau_fallback))
                    exercise_db = r.scalar_one_or_none()
                    if exercise_db:
                        break

            if exercise_db:
                # Envoie le PDF
                from pathlib import Path
                import fitz as _fitz

                pdf_path = Path(exercise_db.exercise_path)
                print(f"  → PDF path: {pdf_path} | exists: {pdf_path.exists()}")
                if pdf_path.exists():
                    # Vérifie que le contenu correspond à la sous-demande (physique vs chimie)
                    pdf_text = ""
                    try:
                        doc = _fitz.open(str(pdf_path))
                        for page in doc:
                            pdf_text += page.get_text("text")
                        doc.close()
                    except Exception:
                        pass

                    demande_lower = text.lower()
                    contenu_ok = True

                    if "physique" in demande_lower and "chimie" not in demande_lower:
                        physique_keywords = [
                            "vitesse", "force", "énergie", "energie", "tension",
                            "courant", "circuit", "optique", "mécanique", "mecanique",
                            "mouvement", "accélération", "acceleration", "électrique", "electrique",
                        ]
                        contenu_ok = any(kw in pdf_text.lower() for kw in physique_keywords)

                    elif "chimie" in demande_lower and "physique" not in demande_lower:
                        chimie_keywords = [
                            "acide", "base", "mol", "réaction", "reaction", "ph",
                            "alcool", "ester", "oxydation", "titrage",
                            "concentration", "solution", "atome", "ion",
                        ]
                        contenu_ok = any(kw in pdf_text.lower() for kw in chimie_keywords)

                    if not contenu_ok:
                        await whatsapp_sender.send_text(
                            phone,
                            f"😔 Je n'ai pas encore d'exercice de *{demande_lower}* disponible.\n\n"
                            f"Pose-moi une question de cours en attendant ! 📚"
                        )
                        return

                    from app.core.settings import get_settings
                    settings = get_settings()

                    # URL directe via Nginx
                    pdf_url = f"http://72.62.4.97/exercises/{exercise_db.matiere}/{pdf_path.name}"
                    print(f"  → PDF URL: {pdf_url}")

                    if pdf_url:
                        annee_str = f" ({exercise_db.annee})" if exercise_db.annee else ""
                        chapitre_str = f" — {exercise_db.chapitre}" if exercise_db.chapitre else ""

                        intro = (
                            f"📝 *Exercice {exercise_db.matiere.upper()}{chapitre_str}{annee_str}*\n\n"
                            f"Voici un exercice adapté à ton niveau.\n\n"
                            f"- Fais l'exercice sur papier ✏️\n"
                            f"- Prends une photo de ta copie 📸\n"
                            f"- Envoie-moi la photo pour que je te corrige\n\n"
                            f"_Bon courage ! 💪_"
                        )
                        await whatsapp_sender.send_text(phone, intro)

                        # Envoie via URL Nginx
                        payload = {
                            "to": phone,
                            "documentUrl": pdf_url,
                            "fileName": pdf_path.name,
                            "text": f"Exercice {exercise_db.matiere}",
                        }
                        await whatsapp_sender._send(payload)

                        # Sauvegarde état
                        user.conversation_state = {
                            "awaiting_copy": True,
                            "exercise_id": str(exercise_db.id),
                            "exercise_path": str(exercise_db.exercise_path),
                            "correction_path": str(exercise_db.correction_path) if exercise_db.correction_path else None,
                            "matiere": detected_matiere,
                            "chapitre": detected_chapitre,
                            "started_at": datetime.now().isoformat(),
                        }
                        await db.flush()
                        print(f"Exercice PDF envoyé -> {phone}: {exercise_db.title}")
                        return

            # Pas d'exercice en DB → message à l'élève
            await whatsapp_sender.send_text(
                phone,
                f"😔 Je n'ai pas encore d'exercice disponible pour *{detected_matiere}*"
                + (f" — *{detected_chapitre}*" if detected_chapitre else "")
                + ".\n\nPose-moi une question de cours en attendant ! 📚"
            )
            return

        # ── Mode normal — appel LLM ───────────────────────────────────
        response = await call_llm(
            user_message=text,
            user_plan=user.plan,
            exam_type=user.exam_type or "",
            subject=detected_matiere,
            series=user.series or "",
            complexity=detect_complexity(text),
            history=history,
            db=db,
            chapitre=detected_chapitre,
            detection=detection,
            user=user,
        )

        await message_repo.save(
            db=db,
            user_id=user.id,
            direction="outbound",
            content=response.text,
            llm_provider=response.provider,
            from_cache=response.from_cache,
        )

        # Met à jour le profil cognitif
        if detected_matiere and detected_chapitre:
            try:
                await mastery_service.update_after_interaction(
                    db=db,
                    user_id=user.id,
                    matiere=detected_matiere,
                    chapitre=detected_chapitre,
                    detection=detection,
                    response_text=response.text,
                )
            except Exception as e:
                print(f"Mastery update error: {e}")

        # Traite la réponse — texte + images via Cloudinary
        blocks = await media_processor.process(response.text)
        for block in blocks:
            if block["type"] == "text":
                await whatsapp_sender.send_text(phone, block["content"])
            elif block["type"] == "image":
                url = await storage_service.upload_image(block["content"])
                if url:
                    await whatsapp_sender.send_image_url(phone, url)
                else:
                    print("Upload Cloudinary echoue — image ignoree")

        print(f"IA ({response.provider}) -> {phone}: {response.text[:80]}...")