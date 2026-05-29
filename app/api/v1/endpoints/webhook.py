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

    if data.get("event") == "messages.received":
        msg = data.get("data", {}).get("messages", {})
        image_msg = msg.get("message", {}).get("imageMessage", {})
        if image_msg:
            print(f"DEBUG imageMessage keys: {list(image_msg.keys())}")
            print(f"DEBUG imageMessage: {str(image_msg)[:300]}")

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

    # Détecte les images (copies manuscrites)
    image_data = None
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
    else:
        # Vérifie si c'est une image
        raw_msg = message.get("message", {}) or {}
        if "imageMessage" in raw_msg:
            image_data = {
                "key": message.get("key", {}),
                "message": raw_msg,
            }
            msg_type = "image"

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

    await handle_onboarding(phone, text, user, db, msg_type=msg_type, image_data=image_data)
    await user_service.increment_message_count(db, user)


async def handle_onboarding(phone: str, text: str, user, db: AsyncSession, msg_type: str = "text", image_data: dict = None):
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
                messages.quota_reached(user.name or "ami"),
                messages.QUOTA_BUTTONS,
            )
            return

        # Traitement copie manuscrite
        if msg_type == "image" and image_data:
            conv_state = user.conversation_state or {}
            if conv_state.get("awaiting_copy"):
                from app.services.copy_analyzer_service import copy_analyzer_service
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

                # Réinitialise l'état
                user.conversation_state = {}
                await db.flush()

                print(f"Copie analysée -> {phone}: score={analysis.get('score')}")
                return
            else:
                # Image reçue sans exercice en cours
                await whatsapp_sender.send_text(
                    phone,
                    "📸 J'ai reçu ton image !\n\n"
                    "Pour que je puisse corriger ta copie, demande-moi d'abord un exercice :\n"
                    "_\"Donne moi un exercice de maths\"_"
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

            # Cherche un exercice PDF en DB
            query = select(Exercise).where(
                Exercise.status == "ready",
                Exercise.exercise_path.isnot(None),
            )
            if user.exam_type:
                query = query.where(Exercise.exam_type == user.exam_type)
            if user.series:
                query = query.where(Exercise.serie == user.series)
            if detected_matiere:
                query = query.where(Exercise.matiere == detected_matiere)
            if detected_chapitre:
                query = query.where(Exercise.chapitre == detected_chapitre)
            query = query.order_by(func.random()).limit(1)

            result = await db.execute(query)
            exercise_db = result.scalar_one_or_none()

            if not exercise_db:
                # Fallback sans chapitre
                query2 = select(Exercise).where(
                    Exercise.status == "ready",
                    Exercise.exercise_path.isnot(None),
                    Exercise.matiere == detected_matiere,
                ).order_by(func.random()).limit(1)
                result2 = await db.execute(query2)
                exercise_db = result2.scalar_one_or_none()

            if exercise_db:
                # Envoie le PDF
                from pathlib import Path

                pdf_path = Path(exercise_db.exercise_path)
                print(f"  → PDF path: {pdf_path} | exists: {pdf_path.exists()}")
                if pdf_path.exists():
                    pdf_bytes = pdf_path.read_bytes()

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

            # Fallback → génère un exercice avec le LLM
            if detected_chapitre:
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
                    await whatsapp_sender.send_text(phone, exercise_data["text"])
                    print(f"Exercice LLM généré -> {phone}: {detected_matiere}/{detected_chapitre}")
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