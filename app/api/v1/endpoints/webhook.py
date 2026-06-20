import json
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends
from sqlalchemy import select as sa_select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.settings import get_settings
from app.db.database import get_db
from app.services.user_service import user_service, generate_referral_code
from app.services.whatsapp.sender import whatsapp_sender
from app.services.whatsapp.messages import messages
from app.services.llm.service import call_llm
from app.repositories.message_repository import message_repo
from app.services.config_service import config_service
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
            import traceback
            print(f"process_message error (phone={message.get('from', '?')}): {e}")
            traceback.print_exc()
            try:
                await db.rollback()
            except Exception:
                pass

    return {"status": "ok"}



def _set_editing(user, usage_key: str) -> None:
    """
    Prépare une édition/ajout de section post-onboarding :
    - marque editing_only=True (retour à 'done' à la fin du flux, pas de chaînage/plan)
    - ajoute la section à user.usage si absente
    """
    conv = dict(user.conversation_state or {})
    conv["editing_only"] = True
    user.conversation_state = conv
    usage = user.usage or []
    if isinstance(usage, str):
        usage = [usage]
    else:
        usage = list(usage)
    if usage_key not in usage:
        usage.append(usage_key)
    user.usage = usage


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
        "action_profil": "progression",
        "/profil": "profil",
        "edit_emploi": "edit_emploi",
        "add_emploi": "add_emploi",
        "confirm_new_service": "confirm_new_service",
        "ignore_service": "ignore_service",
        "petits jobs": "petits_jobs",
        "petit job": "petits_jobs",
        "mes petits jobs": "petits_jobs",
        "les petits jobs": "petits_jobs",
        "petits_jobs": "petits_jobs",
        "petit_job_oui": "petit_job_oui",
        "petit_job_non": "petit_job_non",
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
        msg = messages.profil_complet(user)
        buttons = [{"id": "edit_emploi", "title": "💼 Mon profil emploi"}]
        conv = user.conversation_state or {}
        conv["pending_menu"] = "profil"
        conv["menu_options"] = [b["id"] for b in buttons]
        user.conversation_state = conv
        await db.flush()
        await whatsapp_sender.send_buttons(phone, msg, buttons)

    elif command in ("edit_emploi", "add_emploi"):
        _set_editing(user, "emploi")
        await _start_emploi_conversation(phone, user, db)

    elif command == "confirm_new_service":
        conv = user.conversation_state or {}
        service = conv.get("pending_service")
        conv.pop("pending_service", None)
        conv.pop("service_suggestion_pending", None)
        user.conversation_state = conv
        if service == "emploi":
            _set_editing(user, "emploi")
            await _start_emploi_conversation(phone, user, db)
        else:
            await db.flush()

    elif command == "ignore_service":
        conv = user.conversation_state or {}
        conv.pop("pending_service", None)
        conv.pop("service_suggestion_pending", None)
        user.conversation_state = conv
        await db.flush()

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
            await whatsapp_sender.send_text(phone, messages.plan_message_pro(user))
        else:
            await _send_pro_offer(phone, user, _usage_context(user))

    elif command == "petits_jobs":
        try:
            from app.services.petit_job_service import petit_job_service as _pjs
            _jobs = await _pjs.list_active(db, lieu=getattr(user, "localisation_emploi", None))
            await whatsapp_sender.send_text(phone, messages.petit_job_list(_jobs, user.name or ""))
        except Exception as _e:
            print(f"  [petits_jobs] erreur: {_e}")
            await whatsapp_sender.send_text(phone, "❌ Impossible de charger les petits jobs. Réessaie.")

    elif command == "petit_job_oui":
        conv = user.conversation_state or {}
        if not conv.get("awaiting_petit_job_confirm"):
            return
        draft = conv.get("petit_job_draft", {})
        try:
            from app.services.petit_job_service import petit_job_service as _pjs
            _job = await _pjs.create(db, user.id, draft)
            _nb = await _pjs.notify_candidates(db, _job)
            await db.commit()
            user.conversation_state = {}
            await db.flush()
            await whatsapp_sender.send_text(phone, messages.petit_job_posted(_job.titre, _nb))
        except Exception as _e:
            print(f"  [petit_job_oui] erreur: {_e}")
            await whatsapp_sender.send_text(phone, "❌ Erreur lors de la publication. Réessaie.")

    elif command == "petit_job_non":
        user.conversation_state = {}
        await db.flush()
        await whatsapp_sender.send_text(phone, messages.petit_job_cancelled(user.name or ""))

    elif command == "mes_offres":
        # Affiche les offres d'emploi matchées pour ce user
        try:
            from sqlalchemy import select as _sel_off
            from app.models.candidate_profile import JobMatch as _JM
            from app.models.job_opportunity import JobOpportunity as _JO
            _rows = (await db.execute(
                _sel_off(_JM, _JO)
                .join(_JO, _JM.job_id == _JO.id)
                .where(_JM.user_id == user.id, _JO.statut == "active")
                .order_by(_JM.score_match.desc())
                .limit(5)
            )).all()

            if not _rows:
                await whatsapp_sender.send_text(
                    phone,
                    f"🔍 Aucune offre d'emploi disponible pour toi pour l'instant, {user.name} !\n\n"
                    "Je te notifie dès qu'une opportunité correspondant à ton profil apparaît. 💼"
                )
            else:
                _msg = f"💼 *Tes offres d'emploi, {user.name}* :\n\n"
                for _match, _job in _rows:
                    _deadline = ""
                    if _job.date_limite:
                        try:
                            _deadline = f"\n  📅 Deadline : {_job.date_limite.strftime('%d/%m/%Y')}"
                        except Exception:
                            pass
                    _score = f"{_match.score_match:.0f}%" if _match.score_match else "N/A"
                    _msg += (
                        f"*{_job.titre}*\n"
                        f"  🏢 {_job.entreprise or 'N/A'}{_deadline}\n"
                        f"  🎯 Score matching : {_score}\n\n"
                    )
                _msg += "_Pour postuler, contacte directement l'entreprise ou tape /profil pour voir ton profil complet._"
                await whatsapp_sender.send_text(phone, _msg)
        except Exception as _off_err:
            print(f"  [mes_offres] erreur: {_off_err}")
            await whatsapp_sender.send_text(phone, "❌ Impossible de charger tes offres. Réessaie.")


def _usage_context(user) -> str:
    """Déduit le contexte d'upsell depuis user.usage."""
    usage = user.usage or []
    if isinstance(usage, str):
        usage = [usage]
    s = set(usage)
    if s == {"emploi"}:
        return "emploi"
    if s == {"etudes"}:
        return "etudes"
    if s == {"concours"}:
        return "concours"
    return "tout"


async def _send_pro_offer(phone: str, user, context: str = "tout") -> None:
    """
    Envoie l'offre Pro adaptée au contexte.
    Tente PayDunya ; si le lien échoue → fallback Wave (paiement manuel, activation 24h).
    """
    from app.services.payment_service import payment_service
    payment_url = None
    try:
        invoice = await payment_service.create_invoice(user=user, plan="pro")
        if invoice.get("success") and invoice.get("payment_url"):
            payment_url = invoice["payment_url"]
    except Exception as e:
        print(f"_send_pro_offer PayDunya error: {e}")
    await whatsapp_sender.send_text(phone, messages.pro_upsell(user.name or "toi", context, payment_url))


# Options du menu de services (ordre = numéros 1..5) → commandes internes.
_SERVICES_MENU_OPTIONS = ["mes_offres", "petits_jobs", "profil", "inviter", "plan"]


async def _send_services_menu(phone: str, user, db: AsyncSession) -> None:
    """Envoie le menu fixe des services + arme la navigation numérique (1..5)."""
    conv = user.conversation_state or {}
    conv["pending_menu"] = "services"
    conv["menu_options"] = list(_SERVICES_MENU_OPTIONS)
    user.conversation_state = conv
    await db.flush()
    await whatsapp_sender.send_text(phone, messages.services_menu(user.name or ""))


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
    elif msg_type == "document":
        pass  # message déjà disponible, traité dans handle_onboarding (upload CV)

    if not text and not image_data and msg_type != "document":
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
                        f"Quand il sera actif tu gagneras :\n"
                        f"✅ *+20 messages* bonus\n"
                        f"✅ *+1 offre d'emploi* supplémentaire par semaine 💪"
                    )

    if user.status == "active":
        quota = await user_service.check_quota(user)
        conv_q = user.conversation_state or {}
        menu_pending = conv_q.get("pending_menu")
        menu_options = conv_q.get("menu_options", []) or []

        # Seule la VRAIE navigation de menu (un chiffre ou un id d'option) échappe
        # au quota — une question libre reste bloquée même si un menu traîne.
        is_menu_nav = bool(menu_pending) and (
            text.strip().isdigit() or text.lower().strip() in menu_options
        )

        if not quota["allowed"]:
            text_lower = text.lower().strip()

            # Les choix quota (1/2 + IDs boutons) ont TOUJOURS la priorité,
            # même si un menu profil est en attente (is_menu_nav ne compte pas ici).
            if text_lower in ("action_invite", "1", "inviter des amis", "inviter", "/inviter"):
                await handle_command("inviter", phone, user, db)
                return
            if text_lower in ("action_pro", "2", "passer pro", "pro", "/plan"):
                await handle_command("plan", phone, user, db)
                return

            # Les commandes de consultation restent accessibles (pas de LLM)
            _cmd_over_quota = detect_command(text)
            if _cmd_over_quota in ("profil", "progression", "aide", "inviter"):
                await handle_command(_cmd_over_quota, phone, user, db)
                return

            # Navigation menu légitme (ex: /profil puis choix 3) → autorisée
            if is_menu_nav:
                pass  # laisse passer vers handle_onboarding
            else:
                # Affiche le message quota avec options
                _ctx = _usage_context(user)
                _ctx_quota = _ctx if _ctx == "emploi" else "emploi"
                await whatsapp_sender.send_buttons(
                    phone,
                    messages.quota_reached(user.name or "ami", _ctx_quota),
                    messages.QUOTA_BUTTONS,
                )
                return

    await handle_onboarding(phone, text, user, db, msg_type=msg_type, image_data=image_data, message=message)
    await user_service.increment_message_count(db, user)


def _resolve_num(text: str, ordered_ids: list[str]) -> str | None:
    """
    Convertit "1", "2.", "3 " etc. vers l'ID correspondant dans ordered_ids.
    Retourne None si text n'est pas un numéro valide.
    """
    t = text.strip().rstrip(".")
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(ordered_ids):
            return ordered_ids[idx]
    return None


async def _start_emploi_conversation(phone: str, user, db: AsyncSession):
    """Lance l'onboarding emploi structuré (étape 1 : liste rôle + type)."""
    _conv = user.conversation_state or {}
    _conv["emploi_ob"] = {}
    user.conversation_state = _conv
    user.onboarding_step = "emploi_type_choice"
    await db.flush()
    await whatsapp_sender.send_list(
        phone,
        messages.ask_emploi_role(user.name or "toi"),
        "Choisir",
        messages.EMPLOI_ROLE_SECTIONS,
    )


async def _finalise_emploi(phone: str, user, db: AsyncSession, editing: bool = False, emploi_type_hint: str | None = None):
    """
    Clôture l'onboarding emploi : persiste le profil, déduit emploi_type,
    envoie le message de fin personnalisé et lance le matching.
    """
    from app.models.candidate_profile import CandidateProfile as _CP
    from sqlalchemy import select as _sel

    # Garde-fou anti double-finalise (cause de messages en double).
    if not editing and (user.conversation_state or {}).get("_emploi_finalised"):
        print(f"  [_finalise_emploi] déjà finalisé pour {user.id} — ignoré")
        return

    # Crée ou met à jour le CandidateProfile
    _cp = (await db.execute(_sel(_CP).where(_CP.user_id == user.id))).scalar_one_or_none()
    if not _cp:
        _cp = _CP(
            user_id=user.id,
            secteurs_interets=user.secteur_emploi or [],
            niveau_etudes=user.niveau_etudes,
            localisation=user.localisation_emploi,
            type_contrat_souhaite=user.type_contrat_souhaite,
        )
        db.add(_cp)
        await db.flush()
    else:
        if user.secteur_emploi:
            _cp.secteurs_interets = user.secteur_emploi
        if user.niveau_etudes:
            _cp.niveau_etudes = user.niveau_etudes
        if user.localisation_emploi:
            _cp.localisation = user.localisation_emploi
        if user.type_contrat_souhaite:
            _cp.type_contrat_souhaite = user.type_contrat_souhaite
        await db.flush()

    # Déduction de l'emploi_type
    # Priorité : hint (capté en conversation) → infer LLM → fallback les_deux
    _et = emploi_type_hint or _cp.emploi_type or None
    if _et:
        # Déjà connu depuis la conversation — pas besoin d'un appel LLM supplémentaire
        _cp.emploi_type = _et
        await db.flush()
        print(f"  [emploi_type] user={user.id} → {_et} (conversation)")
    else:
        _et = "les_deux"
        try:
            from app.services.onboarding_llm import infer_emploi_type as _infer
            _et = await _infer(user)
            _cp.emploi_type = _et
            await db.flush()
            print(f"  [emploi_type] user={user.id} → {_et} (infer)")
        except Exception as _err:
            print(f"  [emploi_type] erreur ignorée: {_err}")
            _cp.emploi_type = _et
            await db.flush()

    if editing:
        _conv = user.conversation_state or {}
        _conv.pop("editing_only", None)
        user.conversation_state = _conv
        user.onboarding_step = "done"
        await db.flush()
        await whatsapp_sender.send_text(phone, f"✅ Profil emploi mis à jour *{user.name}* !")
    else:
        await user_service.complete_onboarding(db, user)
        await whatsapp_sender.send_text(
            phone, messages.emploi_done_msg(user.name or "toi", _et)
        )

    # Marque la finalisation (idempotence anti-doublon)
    _conv_fin = user.conversation_state or {}
    _conv_fin["_emploi_finalised"] = True
    user.conversation_state = _conv_fin
    await db.flush()

    # ── CTA finale selon la voie + présence de CV ────────────────────
    # IMPORTANT : emploi_done_msg dit DÉJÀ « tu seras notifié ». On n'envoie donc
    # PAS de second message « je cherche des offres » (c'était le doublon BUG 1).
    has_cv = bool(_cp.cv_url or _cp.cv_text)
    needs_cv = _et in ("entreprise", "les_deux")

    if needs_cv and not has_cv:
        # Voie entreprise = CV obligatoire → relance souple (flag cv_pending).
        _conv_cv = user.conversation_state or {}
        _conv_cv["cv_pending"] = True
        user.conversation_state = _conv_cv
        await db.flush()
        _cta = (
            "_En attendant, tu reçois déjà les petits jobs près de chez toi._"
            if _et == "les_deux"
            else "_Ou tape *petits jobs* pour les missions courtes._"
        )
        await whatsapp_sender.send_text(
            phone,
            f"📄 Pour recevoir les *offres d'entreprise*, envoie ton *CV* (PDF ou photo).\n{_cta}"
        )
    elif needs_cv and has_cv:
        # CV présent → matching entreprise (match_candidate notifie lui-même).
        try:
            from app.services.matching_service import matching_service
            await matching_service.match_candidate(db, user.id)
        except Exception as _me:
            print(f"Matching emploi error: {_me}")
    # Voie petit_job pure → emploi_done_msg contient déjà le CTA « petits jobs ».


async def _ask_usage(phone: str, user, db: AsyncSession):
    """Demande l'usage après confirmation du pays.
    En mode emploi uniquement : saute la question usage et va directement
    au profil emploi (plus d'études ni de concours)."""
    from app.services.platform_mode import is_emploi_only
    if await is_emploi_only():
        user.usage = ["emploi"]
        await _start_emploi_conversation(phone, user, db)
        return

    user.onboarding_step = "usage"
    await db.flush()
    await whatsapp_sender.send_buttons(
        phone,
        messages.ask_usage(user.name) + "\n\n_Tape *4* pour 🎯 Tout à la fois_",
        messages.USAGE_BUTTONS,
    )



async def handle_onboarding(phone: str, text: str, user, db: AsyncSession, msg_type: str = "text", image_data: dict = None, message: dict = None):
    step = user.onboarding_step

    # ── Couche LLM intelligente ───────────────────────────────────────────────
    # Avant chaque étape, on laisse le LLM analyser le message pour :
    #   - Extraire une valeur propre (ex: "moi c'est khady" → "Khady")
    #   - Détecter les cas particuliers (pas de CV, date passée...)
    #   - Répondre aux questions hors flow sans casser l'étape
    # En cas d'erreur LLM, le comportement original est préservé (fallback sécurisé).
    _llm_result = None
    _EMPLOI_STEPS = {"emploi_type_choice", "emploi_secteur_choice", "emploi_niveau_choice", "emploi_localisation"}
    if step not in ("start", "plan", "emploi_cv") and step not in _EMPLOI_STEPS and msg_type == "text":
        try:
            from app.services.onboarding_llm import analyze as _llm_analyze, should_analyze as _should_analyze
            if _should_analyze(step, text):
                _user_ctx = {
                    "nom": user.name,
                    "pays": getattr(user, "pays", None),
                    "usage": getattr(user, "usage", None),
                    "exam_type": getattr(user, "exam_type", None),
                    "plan": getattr(user, "plan", None),
                    "niveau_etudes": getattr(user, "niveau_etudes", None),
                }
                _llm_result = await _llm_analyze(
                    step=step,
                    text=text,
                    user_name=user.name or "",
                    user_context=_user_ctx,
                )
                print(f"  [onboarding_llm] step={step} text={text!r} → {_llm_result}")

                # Si le LLM veut guider, répondre ou clarifier → envoyer message et stopper
                if _llm_result and _llm_result.get("action") in ("guide", "answer", "clarify"):
                    msg_to_send = _llm_result.get("message")
                    if msg_to_send:
                        await whatsapp_sender.send_text(phone, msg_to_send)
                    return  # On reste sur la même étape, le webhook ne va pas plus loin

                # Si le LLM a extrait une valeur propre → remplacer text pour la suite
                if _llm_result and _llm_result.get("action") == "proceed" and _llm_result.get("value"):
                    text = _llm_result["value"]

        except Exception as _llm_err:
            print(f"  [onboarding_llm] erreur ignorée: {_llm_err}")
            _llm_result = None
    # ─────────────────────────────────────────────────────────────────────────

    if step == "start":
        from app.services.platform_mode import is_emploi_only
        _welcome = messages.WELCOME_EMPLOI if await is_emploi_only() else messages.WELCOME
        await whatsapp_sender.send_text(phone, _welcome)
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
            await _ask_usage(phone, user, db)
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

        await _ask_usage(phone, user, db)

    elif step == "usage":
        # Toujours forcer emploi
        user.usage = ["emploi"]
        await _start_emploi_conversation(phone, user, db)

    # ════════════════════════════════════════════════════════════════
    # ONBOARDING EMPLOI STRUCTURÉ — 4 étapes fixes + LLM en support
    # ════════════════════════════════════════════════════════════════

    elif step == "emploi_type_choice":
        # ── Étape 1 : rôle + type de travail (liste 4 options) ──────
        from app.services.onboarding_llm import interpret_emploi_step as _interp

        _conv = user.conversation_state or {}
        _ob = _conv.get("emploi_ob", {})
        _editing = _conv.get("editing_only", False)
        _name = user.name or "toi"

        # IDs liste → (role, emploi_type)
        _ROLE_MAP = {
            "ert_petit_job":  ("demandeur", "petit_job"),
            "ert_entreprise": ("demandeur", "entreprise"),
            "ert_les_deux":   ("demandeur", "les_deux"),
            "ert_offreur":    ("offreur",   None),
        }
        _ROLE_ORDER = ["ert_petit_job", "ert_entreprise", "ert_les_deux", "ert_offreur"]

        # Résoudre "1"/"2"/"3"/"4" vers l'ID correspondant
        _resolved = _resolve_num(text, _ROLE_ORDER)
        if _resolved:
            text = _resolved

        _role, _et = _ROLE_MAP.get(text, (None, None))

        if _role is None and msg_type == "text" and text:
            # Texte libre → LLM interprète
            _i = await _interp("emploi_role", text)
            _mv = _i.get("mapped_value")
            if _mv == "offreur":
                _role, _et = "offreur", None
            elif _mv in ("petit_job", "entreprise", "les_deux"):
                _role, _et = "demandeur", _mv
            else:
                # LLM guide : envoyer message contextuel + re-afficher la liste
                _guide = _i.get("guide_message") or messages.ask_emploi_role(_name)
                await whatsapp_sender.send_list(phone, _guide, "Choisir", messages.EMPLOI_ROLE_SECTIONS)
                return

        if _role is None:
            # Image, audio, réponse incompréhensible → re-afficher la liste
            await whatsapp_sender.send_list(
                phone, messages.ask_emploi_role(_name), "Choisir", messages.EMPLOI_ROLE_SECTIONS
            )
            return

        # ── Offreur : lancer le flux post-job directement ───────────
        if _role == "offreur":
            user.usage = list(set((user.usage or []) + ["emploi"]))
            _conv["awaiting_petit_job_offer"] = True
            user.conversation_state = _conv
            await db.flush()
            await user_service.complete_onboarding(db, user)
            await whatsapp_sender.send_text(
                phone,
                "Décris le job : type de travail, lieu, durée et paie.\n"
                "_Ex : Besoin d'un livreur à moto à Dakar, samedi matin, 5000 FCFA_"
            )
            return

        # ── Chercheur : stocker et passer à l'étape secteur ─────────
        _ob["emploi_type"] = _et
        _conv["emploi_ob"] = _ob
        user.conversation_state = _conv
        user.onboarding_step = "emploi_secteur_choice"
        await db.flush()

        if _et == "petit_job":
            await whatsapp_sender.send_list(
                phone, messages.ask_secteur_petit_job(_name), "Choisir", messages.SECTEUR_PJ_SECTIONS
            )
        elif _et == "entreprise":
            await whatsapp_sender.send_list(
                phone, messages.ask_secteur_entreprise(_name), "Choisir", messages.SECTEUR_ENT_SECTIONS
            )
        else:
            await whatsapp_sender.send_list(
                phone, messages.ask_secteur_les_deux(_name), "Choisir", messages.SECTEUR_LES_DEUX_SECTIONS
            )

    elif step == "emploi_secteur_choice":
        # ── Étape 2 : secteur ───────────────────────────────────────
        from app.services.onboarding_llm import interpret_emploi_step as _interp

        _conv = user.conversation_state or {}
        _ob = _conv.get("emploi_ob", {})
        _editing = _conv.get("editing_only", False)
        _name = user.name or "toi"
        _et = _ob.get("emploi_type", "les_deux")

        # Mapping IDs liste → valeur secteur
        _SECTEUR_MAP = {
            "spj_livraison":    "livraison",
            "spj_vente":        "vente",
            "spj_nettoyage":    "nettoyage",
            "spj_manutention":  "manutention",
            "spj_gardiennage":  "gardiennage",
            "spj_restauration": "restauration",
            "spj_bricolage":    "bricolage",
            "se_info":          "informatique",
            "se_finance":       "finance",
            "se_marketing":     "marketing",
            "se_sante":         "santé",
            "se_btp":           "btp",
            "se_education":     "éducation",
            "se_droit":         "droit",
            "se_rh":            "rh",
        }
        _AUTRE_IDS = {"spj_autre", "se_autre"}

        # Ordre des IDs par liste (pour résolution numérique)
        _SECTEUR_ORDER = {
            "petit_job":  ["spj_livraison", "spj_vente", "spj_nettoyage", "spj_manutention",
                           "spj_gardiennage", "spj_restauration", "spj_bricolage", "spj_autre"],
            "entreprise": ["se_info", "se_finance", "se_marketing", "se_sante",
                           "se_btp", "se_education", "se_droit", "se_rh", "se_autre"],
            "les_deux":   ["spj_livraison", "spj_vente", "spj_nettoyage", "spj_manutention",
                           "spj_gardiennage", "spj_restauration",
                           "se_info", "se_finance", "se_marketing", "se_sante",
                           "se_btp", "se_education", "se_droit", "se_autre"],
        }
        _resolved = _resolve_num(text, _SECTEUR_ORDER.get(_et, []))
        if _resolved:
            text = _resolved

        _secteur = _SECTEUR_MAP.get(text)

        # Helper : renvoyer la liste secteur adaptée (avec guide optionnel avant)
        async def _retry_secteur(guide: str | None = None):
            if guide:
                await whatsapp_sender.send_text(phone, guide)
            if _et == "petit_job":
                await whatsapp_sender.send_list(phone, messages.ask_secteur_petit_job(_name), "Choisir", messages.SECTEUR_PJ_SECTIONS)
            elif _et == "entreprise":
                await whatsapp_sender.send_list(phone, messages.ask_secteur_entreprise(_name), "Choisir", messages.SECTEUR_ENT_SECTIONS)
            else:
                await whatsapp_sender.send_list(phone, messages.ask_secteur_les_deux(_name), "Choisir", messages.SECTEUR_LES_DEUX_SECTIONS)

        if not _secteur and text in _AUTRE_IDS:
            # "Autre" → LLM demande de préciser via un step dédié
            _step_key = "secteur_autre_pj" if _et == "petit_job" else "secteur_autre_ent"
            _ob["awaiting_autre_secteur"] = _step_key
            _conv["emploi_ob"] = _ob
            user.conversation_state = _conv
            await db.flush()
            _q = "C'est quel type de boulot ? _(ex: jardinage, baby-sitting...)_" if _et == "petit_job" \
                else "C'est quel domaine ? _(ex: communication, audit...)_"
            await whatsapp_sender.send_text(phone, _q)
            return

        # Texte libre "Autre" en cours (awaiting_autre_secteur) OU texte ordinaire
        if not _secteur:
            _awaiting_autre = _ob.get("awaiting_autre_secteur")
            if _awaiting_autre and msg_type == "text" and text:
                _i = await _interp(_awaiting_autre, text)
                _secteur = _i.get("mapped_value")
                if not _secteur:
                    _guide = _i.get("guide_message") or (
                        "Décris le boulot en quelques mots _(ex: jardinage, garderie...)_"
                        if _awaiting_autre == "secteur_autre_pj"
                        else "Précise le domaine en quelques mots _(ex: communication, audit...)_"
                    )
                    await whatsapp_sender.send_text(phone, _guide)
                    return
                # "Autre" bien précisé → nettoyer flag
                _ob.pop("awaiting_autre_secteur", None)
            elif msg_type == "text" and text:
                _step_key = "secteur_petit_job" if _et == "petit_job" else "secteur_entreprise"
                _i = await _interp(_step_key, text)
                _secteur = _i.get("mapped_value")
                if not _secteur:
                    # Guide LLM + re-afficher la liste
                    await _retry_secteur(_i.get("guide_message"))
                    return
            else:
                # Image, audio, réponse non-texte → re-afficher la liste
                await _retry_secteur()
                return

        if not _secteur:
            await _retry_secteur()
            return

        # Stocker secteur
        _ob["secteur"] = _secteur
        user.secteur_emploi = [_secteur]

        # Petit job pur → sauter le niveau d'études, aller à la localisation
        if _et == "petit_job":
            _ob["niveau_etudes"] = "aucun"   # défaut pour petit job
            _conv["emploi_ob"] = _ob
            user.conversation_state = _conv
            user.onboarding_step = "emploi_localisation"
            await db.flush()
            await whatsapp_sender.send_text(phone, messages.ask_localisation_onboarding(_name))
        else:
            # Entreprise / les_deux → demander niveau d'études
            _conv["emploi_ob"] = _ob
            user.conversation_state = _conv
            user.onboarding_step = "emploi_niveau_choice"
            await db.flush()
            await whatsapp_sender.send_list(
                phone,
                messages.ask_niveau_emploi(_name),
                "Choisir",
                messages.NIVEAU_EMPLOI_SECTIONS,
            )

    elif step == "emploi_niveau_choice":
        # ── Étape 3 : niveau d'études ────────────────────────────────
        from app.services.onboarding_llm import interpret_emploi_step as _interp

        _conv = user.conversation_state or {}
        _ob = _conv.get("emploi_ob", {})
        _editing = _conv.get("editing_only", False)
        _name = user.name or "toi"

        _NIV_MAP = {
            "niv_aucun": "aucun",
            "niv_bac":   "bac",
            "niv_bac2":  "bac+2",
            "niv_bac3":  "bac+3",
            "niv_bac5":  "bac+5",
            "niv_doc":   "doctorat",
        }
        _NIV_ORDER = ["niv_aucun", "niv_bac", "niv_bac2", "niv_bac3", "niv_bac5", "niv_doc"]

        _resolved = _resolve_num(text, _NIV_ORDER)
        if _resolved:
            text = _resolved

        _niv = _NIV_MAP.get(text)

        async def _retry_niveau(guide: str | None = None):
            if guide:
                await whatsapp_sender.send_text(phone, guide)
            await whatsapp_sender.send_list(phone, messages.ask_niveau_emploi(_name), "Choisir", messages.NIVEAU_EMPLOI_SECTIONS)

        if not _niv and msg_type == "text" and text:
            _i = await _interp("niveau_etudes", text)
            _niv = _i.get("mapped_value")
            if not _niv:
                await _retry_niveau(_i.get("guide_message"))
                return

        if not _niv:
            await _retry_niveau()
            return

        # Stocker niveau et déduire emploi_type final intelligemment
        _ob["niveau_etudes"] = _niv
        user.niveau_etudes = _niv

        _et = _ob.get("emploi_type", "les_deux")
        _secteur = _ob.get("secteur", "")
        _SECTEURS_PJ = {"livraison", "manutention", "vente", "nettoyage", "gardiennage", "restauration", "bricolage"}
        _SECTEURS_ENT = {"informatique", "finance", "marketing", "santé", "btp", "éducation", "droit", "rh"}
        _niv_qualifie = _niv in ("bac+2", "bac+3", "bac+5", "doctorat")

        if _et == "les_deux":
            # Affiner depuis niveau + secteur
            if _secteur in _SECTEURS_PJ and not _niv_qualifie:
                _et = "petit_job"
            elif _secteur in _SECTEURS_ENT and _niv_qualifie:
                _et = "entreprise"
            # sinon reste les_deux

        _ob["emploi_type"] = _et
        _conv["emploi_ob"] = _ob
        user.conversation_state = _conv
        user.onboarding_step = "emploi_localisation"
        await db.flush()
        await whatsapp_sender.send_text(phone, messages.ask_localisation_onboarding(_name))

    elif step == "emploi_localisation":
        # ── Étape 4 : localisation (texte libre) ────────────────────
        from app.services.onboarding_llm import interpret_emploi_step as _interp

        _conv = user.conversation_state or {}
        _ob = _conv.get("emploi_ob", {})
        _editing = _conv.get("editing_only", False)
        _name = user.name or "toi"

        if msg_type not in ("text",) or not text:
            await whatsapp_sender.send_text(phone, messages.ask_localisation_onboarding(_name))
            return

        # LLM normalise la ville
        _i = await _interp("localisation", text)
        _loc = _i.get("mapped_value")

        if not _loc:
            # Guide contextuel LLM + re-demander la question (toujours les deux)
            _guide = _i.get("guide_message")
            if _guide:
                await whatsapp_sender.send_text(phone, _guide)
            await whatsapp_sender.send_text(phone, messages.ask_localisation_onboarding(_name))
            return

        # Stocker localisation
        _ob["localisation"] = _loc
        user.localisation_emploi = _loc
        _et = _ob.get("emploi_type", "les_deux")
        _conv["emploi_ob"] = _ob
        user.conversation_state = _conv
        await db.flush()

        # needs_cv : entreprise ou les_deux avec niveau bac+2+
        _niv = _ob.get("niveau_etudes", "aucun")
        _needs_cv = (_et in ("entreprise", "les_deux")) and (_niv in ("bac+2", "bac+3", "bac+5", "doctorat"))

        if _needs_cv:
            user.onboarding_step = "emploi_cv"
            await db.flush()
            await whatsapp_sender.send_text(phone, messages.ask_cv_upload(_name))
        else:
            await _finalise_emploi(phone, user, db, editing=_editing, emploi_type_hint=_et)

    elif step == "emploi_cv":
        _conv_cv = user.conversation_state or {}
        _editing_cv = _conv_cv.get("editing_only", False)
        # Récupère emploi_type depuis le nouvel emploi_ob ou l'ancien collected (compat.)
        _et_hint = (_conv_cv.get("emploi_ob") or {}).get("emploi_type") \
                   or (_conv_cv.get("collected") or {}).get("emploi_type")

        async def _finish_emploi():
            """Clôture la section emploi après CV."""
            await _finalise_emploi(phone, user, db, editing=_editing_cv, emploi_type_hint=_et_hint)

        # ── LLM pour emploi_cv (texte uniquement) ────────────────────────────
        if msg_type == "text":
            try:
                from app.services.onboarding_llm import analyze as _cv_analyze, should_analyze as _cv_should
                if _cv_should("emploi_cv", text):
                    _cv_ctx = {"nom": user.name, "niveau_etudes": getattr(user, "niveau_etudes", None)}
                    _cv_llm = await _cv_analyze("emploi_cv", text, user.name or "", _cv_ctx)
                    print(f"  [onboarding_llm] step=emploi_cv → {_cv_llm}")
                    if _cv_llm and _cv_llm.get("action") in ("guide", "answer", "clarify"):
                        msg_cv = _cv_llm.get("message")
                        if msg_cv:
                            await whatsapp_sender.send_text(phone, msg_cv)
                        return
                    if _cv_llm and _cv_llm.get("action") == "proceed" and _cv_llm.get("value"):
                        text = _cv_llm["value"]
            except Exception as _cv_err:
                print(f"  [onboarding_llm] emploi_cv error ignorée: {_cv_err}")

        if text.lower().strip() in ("passer", "skip", "plus tard", "non", "pas de cv", "j'ai pas"):
            from app.models.candidate_profile import CandidateProfile as _CP
            from sqlalchemy import select as _sel
            _existing = (await db.execute(
                _sel(_CP).where(_CP.user_id == user.id)
            )).scalar_one_or_none()
            if not _existing:
                _min_profile = _CP(
                    user_id=user.id,
                    secteurs_interets=user.secteur_emploi or [],
                    niveau_etudes=user.niveau_etudes,
                    localisation=user.localisation_emploi,
                    type_contrat_souhaite=user.type_contrat_souhaite,
                )
                db.add(_min_profile)
                await db.flush()
            await _finish_emploi()
            return

        if msg_type in ("document", "image") and (image_data or message):
            # Télécharge (via Wasender /decrypt-media) + analyse réellement le CV
            # (sinon cv_url resterait vide et la voie entreprise bloquée à tort).
            _cv_ok = False
            _diag = ""
            try:
                from app.services.whatsapp import media_download as _md
                _media = await _md.download_media(message)
                if _media and _media.get("bytes"):
                    from app.services.cv_processor_service import cv_processor_service as cv_processor
                    _res = await cv_processor.process_cv(
                        db=db, user=user,
                        file_bytes=_media["bytes"],
                        filename=_media.get("filename") or "cv.pdf",
                    )
                    _cv_ok = bool(_res and _res.get("success"))
                    if not _cv_ok:
                        _diag = f"lecture PDF échouée ({len(_media['bytes'])} octets)"
                else:
                    _diag = _md.get_last_error() or "téléchargement échoué"
            except Exception as _cv_dl_err:
                _diag = f"exception {type(_cv_dl_err).__name__}: {str(_cv_dl_err)[:120]}"
                print(f"  [cv upload] erreur: {_cv_dl_err}")

            if _cv_ok:
                _profil = (_res or {}).get("profil", {})
                _competences = (_profil.get("competences_normalisees") or _profil.get("competences") or [])
                _secteurs = _profil.get("secteurs_interets") or []
                _niveau = _profil.get("niveau_etudes") or ""
                _lines = ["✅ CV analysé ! Voici ce que j'ai retenu :\n"]
                if _niveau:
                    _lines.append(f"🎓 Niveau : {_niveau}")
                if _secteurs:
                    _lines.append(f"📂 Secteur(s) : {', '.join(_secteurs[:3])}")
                if _competences:
                    _lines.append(f"🛠️ Compétences : {', '.join(_competences[:5])}")
                _lines.append("\nJe cherche les meilleures offres pour toi. 🎯")
                await whatsapp_sender.send_text(phone, "\n".join(_lines))
                await _finish_emploi()
            else:
                # Honnête + DIAGNOSTIC TEMPORAIRE (à retirer une fois réglé).
                await whatsapp_sender.send_text(
                    phone,
                    "😕 Je n'ai pas réussi à lire ton CV. Renvoie-le en *PDF* "
                    "(ou une photo nette), ou tape *passer* pour continuer sans.\n\n"
                    f"_diag : {_diag}_"
                )
            return

        await whatsapp_sender.send_text(
            phone,
            "📄 Envoie ton CV en *PDF* ou *photo*.\n_Tape *passer* pour continuer sans CV._"
        )

    elif step == "plan":
        # L'utilisateur tape une commande (/profil, /aide…) au lieu de choisir un plan
        cmd_at_plan = detect_command(text)
        is_pro_choice = text in ("onboarding_pro", "action_pro")
        is_free_choice = text in ("onboarding_free", "action_free", "gratuit")

        if cmd_at_plan and not is_pro_choice and not is_free_choice:
            # Termine l'onboarding en gratuit puis exécute la commande
            user = await user_service.complete_onboarding(db, user)
            await handle_command(cmd_at_plan, phone, user, db)
            return

        user = await user_service.complete_onboarding(db, user)

        usage_ob = user.usage or []
        if isinstance(usage_ob, str):
            usage_ob = [usage_ob]
        _is_emploi = "emploi" in usage_ob or "tout" in usage_ob

        if _is_emploi and is_free_choice:
            await whatsapp_sender.send_text(
                phone,
                f"✅ Tout est prêt *{user.name}* !\n\n"
                f"Tu peux maintenant :\n"
                f"• Recevoir des offres d'emploi adaptées 💼\n\n"
                f"Merci de patienter pendant le matching !"
            )
        elif _is_emploi and is_pro_choice:
            await _send_pro_offer(phone, user, _usage_context(user))
        else:
            days_left = 0
            if user.exam_date:
                exam_date = user.exam_date.replace(tzinfo=None)
                days_left = max(0, (exam_date - datetime.now()).days)
            await whatsapp_sender.send_text(
                phone,
                messages.onboarding_complete(user.name, days_left, user.usage)
            )
            if is_pro_choice:
                await _send_pro_offer(phone, user, _usage_context(user))

        # ── Matching emploi après onboarding ─────────────────────────
        # match_candidate envoie lui-même les notifications WhatsApp (3 couches + quota)
        if "emploi" in usage_ob or "tout" in usage_ob:
            try:
                from app.services.matching_service import matching_service
                matches = await matching_service.match_candidate(db, user.id)
                if not matches:
                    await whatsapp_sender.send_text(
                        phone,
                        "🔍 Je cherche activement des offres correspondant à ton profil.\n"
                        "Tu seras notifié dès qu'une opportunité compatible apparaît ! 💼"
                    )
            except Exception as e:
                print(f"Matching emploi onboarding error: {e}")

    elif step == "done":
        quota = await user_service.check_quota(user)
        if not quota["allowed"]:
            # Gère les choix quota ici aussi (sécurité si le premier bloc a laissé passer)
            _tq = (text or "").lower().strip()
            if _tq in ("action_invite", "1", "inviter des amis", "inviter", "/inviter"):
                await handle_command("inviter", phone, user, db)
                return
            if _tq in ("action_pro", "2", "passer pro", "pro", "/plan"):
                await handle_command("plan", phone, user, db)
                return
            await whatsapp_sender.send_buttons(
                phone,
                messages.quota_reached(user.name or "ami", _usage_context(user)),
                messages.QUOTA_BUTTONS,
            )
            return

        # ── Guard : images et documents ignorés (emploi only) ──────────────
        if msg_type in ("image", "document"):
            await whatsapp_sender.send_text(
                phone,
                f"💼 Je suis ton assistant emploi *{user.name or ''}* !\n\n"
                "Je ne traite pas les photos ou documents.\n\n"
                "Tape *mes offres* pour voir tes offres d'emploi 💼"
            )
            return

        # ── Offreur en onboarding : réception de la description du job ──────────
        conv_state = user.conversation_state or {}
        if conv_state.get("awaiting_petit_job_offer") and msg_type == "text":
            conv_state.pop("awaiting_petit_job_offer", None)
            user.conversation_state = conv_state
            await db.flush()
            # Réutilise le même flow que post_job via bot_intelligence
            from app.services.petit_job_service import petit_job_service as _pjs_ob
            _draft_ob = await _pjs_ob.extract_from_text(text, user)
            if _draft_ob:
                conv_state["awaiting_petit_job_confirm"] = True
                conv_state["petit_job_draft"] = _draft_ob
                user.conversation_state = conv_state
                await db.flush()
                await whatsapp_sender.send_buttons(
                    phone,
                    messages.petit_job_confirm(_draft_ob, user.name or ""),
                    messages.PETIT_JOB_CONFIRM_BUTTONS,
                )
            else:
                await whatsapp_sender.send_text(
                    phone,
                    "Je n'ai pas bien compris. Décris le job : type de travail, lieu, durée et paie."
                )
            await user_service.increment_message_count(db, user)
            return

        # ── Confirmation petit job (état awaiting_petit_job_confirm) ──────────
        conv_state = user.conversation_state or {}
        if conv_state.get("awaiting_petit_job_confirm"):
            _tj = (text or "").lower().strip()
            _yes = {"petit_job_oui", "oui", "confirme", "confirmer", "1", "yes", "ok", "c'est bon"}
            _no  = {"petit_job_non", "non", "annule", "annuler", "2", "no", "cancel"}
            if _tj in _yes:
                await handle_command("petit_job_oui", phone, user, db)
                await user_service.increment_message_count(db, user)
                return
            elif _tj in _no:
                await handle_command("petit_job_non", phone, user, db)
                await user_service.increment_message_count(db, user)
                return
            else:
                draft = conv_state.get("petit_job_draft", {})
                await whatsapp_sender.send_buttons(
                    phone,
                    messages.petit_job_confirm(draft, user.name or ""),
                    messages.PETIT_JOB_CONFIRM_BUTTONS,
                )
                return

        # Réponse à un menu en attente (/profil ou menu services) — interprète "1/2/3"
        conv_state = user.conversation_state or {}
        if conv_state.get("pending_menu") in ("profil", "services"):
            menu_options = conv_state.get("menu_options", [])
            raw = text.lower().strip()
            cmd = None
            # Choix par numéro
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(menu_options):
                    cmd = menu_options[idx]
            # Choix par id direct (clic bouton)
            elif raw in menu_options:
                cmd = raw
            if cmd:
                conv_state["pending_menu"] = None
                conv_state["menu_options"] = None
                user.conversation_state = conv_state
                await db.flush()
                await handle_command(cmd, phone, user, db)
                await user_service.increment_message_count(db, user)
                return

        # Détecte les commandes spéciales
        command = detect_command(text)
        if command:
            # Toute commande explicite annule un menu en attente
            if conv_state.get("pending_menu"):
                conv_state["pending_menu"] = None
                conv_state["menu_options"] = None
                user.conversation_state = conv_state
                await db.flush()
            await handle_command(command, phone, user, db)
            await user_service.increment_message_count(db, user)
            return

        # ── Routeur conversationnel (post-onboarding) ──────────────────
        # Le LLM COMPREND et ROUTE ; il ne génère pas de contenu libre. Le code
        # valide, persiste, exécute et rend les faits. Tout finit sur une action
        # de service ou le menu fixe. Si le routeur échoue (None) → menu services.
        if msg_type == "text" and text and len(text.strip()) > 2:
            try:
                from app.services import conversation_brain as _brain
                _hist = await message_repo.get_history(db, user.id, limit=6)
                _decision = await _brain.brain_decide(text=text, user=user, db=db, history=_hist)
            except Exception as _brain_err:
                print(f"  [conversation_brain] erreur ignorée: {_brain_err}")
                _decision = None

            if _decision:
                _action = _decision.get("action", "show_menu")
                _reply = _decision.get("reply")
                _updates = _decision.get("profile_updates") or {}

                # 1) Persiste les champs profil validés (corrections incluses)
                _profile_changed = False
                if _updates:
                    _profile_changed = await _brain.apply_profile_updates(db, user, _updates)
                    if _profile_changed:
                        await _brain.rematch(db, user)

                # 2) Message court du routeur (déjà capé à ~280 car.)
                if _reply:
                    await whatsapp_sender.send_text(phone, _reply)
                # Confirmation systématique si correction/màj sans message explicite
                elif _profile_changed:
                    await whatsapp_sender.send_text(phone, "✅ C'est noté, ton profil est à jour !")

                # 3) Action de SERVICE (réutilise les handlers existants)
                if _action == "show_jobs":
                    await handle_command("mes_offres", phone, user, db)
                elif _action == "show_petit_jobs":
                    await handle_command("petits_jobs", phone, user, db)
                elif _action == "show_profile":
                    await handle_command("profil", phone, user, db)
                elif _action == "show_plan":
                    await handle_command("plan", phone, user, db)
                elif _action == "show_invite":
                    await handle_command("inviter", phone, user, db)
                elif _action == "post_job":
                    from app.services.petit_job_service import petit_job_service as _pjs
                    _draft = await _pjs.extract_from_text(text, user)
                    if _draft:
                        _conv = user.conversation_state or {}
                        _conv["awaiting_petit_job_confirm"] = True
                        _conv["petit_job_draft"] = _draft
                        user.conversation_state = _conv
                        await db.flush()
                        await whatsapp_sender.send_buttons(
                            phone,
                            messages.petit_job_confirm(_draft, user.name or ""),
                            messages.PETIT_JOB_CONFIRM_BUTTONS,
                        )
                    elif not _reply:
                        await whatsapp_sender.send_text(
                            phone,
                            f"📝 Décris le travail que tu proposes *{user.name or ''}* !\n\n"
                            "_Exemple : J'ai besoin de 2 livreurs à moto demain à Dakar Plateau, 5000F la journée_"
                        )
                elif _action == "show_menu":
                    # Le menu est le point d'ancrage permanent : on l'affiche
                    # toujours (après l'éventuelle phrase courte), sauf si on vient
                    # juste de confirmer une correction de profil.
                    if not _profile_changed:
                        await _send_services_menu(phone, user, db)
                # action "none" sans rien → menu aussi.
                elif _action == "none" and not _reply and not _profile_changed:
                    await _send_services_menu(phone, user, db)

                await user_service.increment_message_count(db, user)
                await message_repo.save(db=db, user_id=user.id, direction="inbound", content=text, intent=_decision.get("intent") or "emploi")
                if _reply:
                    await message_repo.save(db=db, user_id=user.id, direction="outbound", content=_reply, intent="emploi")
                return
        # ─────────────────────────────────────────────────────────────

        # ── Fallback : routeur muet → menu fixe (jamais de pavé libre) ──
        if msg_type == "text" and text:
            await _send_services_menu(phone, user, db)
            await message_repo.save(db=db, user_id=user.id, direction="inbound", content=text, intent="emploi")
