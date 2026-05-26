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
from app.db.redis import get_redis
from app.models.user import User as UserModel

settings = get_settings()
router = APIRouter()


@router.get("/webhook")
async def webhook_verify(request: Request):
    return {"status": "ok"}


@router.post("/webhook/debug")
async def webhook_debug(request: Request):
    data = await request.json()
    print(f"DEBUG WEBHOOK: {data}")
    return {"received": data}


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

    # Wasender envoie event="messages.received" avec data.messages
    if event in ("messages.received", "messages-personal.received", "messages.upsert"):
        msg_data = data.get("data", {}).get("messages", {})
        if msg_data and not msg_data.get("key", {}).get("fromMe", False):
            # Normalise le format pour process_message
            incoming = [{
                "from": msg_data.get("key", {}).get("cleanedSenderPn", ""),
                "type": "text",
                "body": msg_data.get("messageBody", ""),
                "id": msg_data.get("key", {}).get("id", ""),
                "fromMe": False,
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
            await redis.setex(cache_key, 300, "1")

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
        msg = messages.plan_message(user)
        if user.plan != "pro":
            await whatsapp_sender.send_buttons(
                phone,
                msg,
                [
                    {"id": "action_pro", "title": "Passer Pro ⭐"},
                    {"id": "action_invite", "title": "Inviter des amis"},
                ]
            )
        else:
            await whatsapp_sender.send_text(phone, msg)


async def process_message(message: dict, db: AsyncSession):
    phone = message.get("from")
    msg_type = message.get("type", "text")

    if not phone:
        return

    text = ""
    if msg_type == "text":
        # Wasender : le corps du message est dans "body" directement
        text = message.get("body", "").strip()
    elif msg_type == "interactive":
        # Wasender interactive (si supporté) — même structure que Meta
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive["button_reply"].get("id", "")
        elif interactive.get("type") == "list_reply":
            text = interactive["list_reply"].get("id", "")
    elif msg_type == "button":
        # Certains providers renvoient les réponses bouton comme type "button"
        text = message.get("button", {}).get("payload", "") or message.get("body", "")

    if not text:
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
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami", user.referral_code or ""),
                messages.QUOTA_BUTTONS,
            )
            return

    await handle_onboarding(phone, text, user, db)
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
            # set_exam_date passe onboarding_step à "plan"
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
        # Si l'élève veut passer Pro directement → envoie le lien de paiement
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
                messages.quota_reached(user.name or "ami", user.referral_code or ""),
                messages.QUOTA_BUTTONS,
            )
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

        # Demande d'exercice → génère un nouvel exercice
        if detection.get("type_demande") == "exercice" and detected_matiere and detected_chapitre:
            from app.services.rag.exercise_generator import exercise_generator

            exercise_data = await exercise_generator.generate_exercise(
                db=db,
                user_id=user.id,
                matiere=detected_matiere,
                chapitre=detected_chapitre,
                exam_type=user.exam_type or "bac_senegal",
                serie=user.series or "S2",
            )

            if exercise_data and exercise_data.get("text"):
                user.conversation_state = {
                    "awaiting_answer": True,
                    "exercise_text": exercise_data["text"],
                    "exercise_type": exercise_data["type"],
                    "matiere": detected_matiere,
                    "chapitre": detected_chapitre,
                    "hints_asked": 0,
                    "niveau": exercise_data["niveau"],
                    "started_at": datetime.now().isoformat(),
                }
                await db.flush()

                await message_repo.save(
                    db=db,
                    user_id=user.id,
                    direction="outbound",
                    content=exercise_data["text"],
                    llm_provider="exercise_generator",
                    from_cache=False,
                )

                await whatsapp_sender.send_text(phone, exercise_data["text"])
                print(f"Exercice généré -> {phone}: {detected_matiere}/{detected_chapitre} niveau={exercise_data['niveau']}")
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