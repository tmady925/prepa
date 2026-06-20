from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import get_db
from app.models.user import User
from app.services.payment_service import payment_service
from app.services.referral_service import referral_service
from app.services.whatsapp.sender import whatsapp_sender

router = APIRouter()


@router.post("/payments/ipn")
async def paydunya_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Reçoit la notification de PayDunya après paiement."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "invalid_payload"}

    print(f"PayDunya webhook: {data.get('status')} token={data.get('token', '')[:12]}...")

    status = data.get("status", "")
    if status.lower() != "completed":
        return {"status": "ignored"}

    custom = data.get("custom_data", {}) or {}
    phone = custom.get("phone")
    token = data.get("token", "")
    payment_method = data.get("payment_method", "")

    if not phone:
        print(f"PayDunya webhook: champ 'phone' absent dans custom_data — token={token[:12]}")
        return {"status": "no_phone"}

    if not token:
        return {"status": "no_token"}

    result = await db.execute(
        select(User).where(User.phone_number == phone)
    )
    user = result.scalar_one_or_none()

    if not user:
        print(f"PayDunya webhook: utilisateur introuvable pour phone={phone}")
        return {"status": "user_not_found"}

    try:
        await payment_service.activate_pro(
            db=db,
            user=user,
            paydunya_token=token,
            payment_method=payment_method,
            raw_data=data,
        )

        activated = await referral_service.activate_paid_bonus(db=db, user=user)
        if activated:
            print(f"Bonus parrainage payant activé pour filleul {phone}")

        await db.commit()
    except Exception as e:
        print(f"PayDunya webhook: erreur activation Pro pour {phone} — {e}")
        await db.rollback()
        return {"status": "activation_error"}

    # Notifie l'élève sur WhatsApp (hors transaction)
    try:
        await whatsapp_sender.send_text(
            phone,
            f"🎉 Félicitations *{user.name}* !\n\n"
            f"Ton abonnement *Prepa Pro* est activé pour 30 jours.\n\n"
            f"Tu peux maintenant réviser sans limite. Bonne chance ! 💪"
        )
    except Exception as e:
        print(f"PayDunya webhook: notification WhatsApp échouée pour {phone} — {e}")

    return {"status": "ok"}


@router.post("/payments/recruiter-ipn")
async def recruiter_paydunya_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Callback PayDunya après paiement d'un recruteur."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "invalid_payload"}

    print(f"Recruiter PayDunya IPN: {data.get('status')} token={data.get('token', '')[:12]}...")

    status = data.get("status", "")
    if status.lower() != "completed":
        return {"status": "ignored"}

    custom = data.get("custom_data", {}) or {}
    if custom.get("type") != "recruiter":
        return {"status": "not_recruiter"}

    recruiter_id = custom.get("recruiter_id")
    plan = custom.get("plan", "starter")
    token = data.get("token", "")

    if not recruiter_id or not token:
        return {"status": "missing_data"}

    from app.models.recruiter import Recruiter
    import uuid as _uuid
    from datetime import datetime, timezone, timedelta
    try:
        r_uuid = _uuid.UUID(recruiter_id)
    except ValueError:
        return {"status": "invalid_recruiter_id"}

    result = await db.execute(select(Recruiter).where(Recruiter.id == r_uuid))
    recruiter = result.scalar_one_or_none()
    if not recruiter:
        print(f"Recruiter IPN: recruteur {recruiter_id} introuvable")
        return {"status": "recruiter_not_found"}

    # starter / pro → annonces illimitées (None), abonnement 30j
    try:
        recruiter.plan = plan
        recruiter.statut = "active"
        recruiter.annonces_restantes = None  # illimité pour les plans payants
        recruiter.abonnement_expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        recruiter.paydunya_token = token
        await db.commit()
        print(f"Recruiter plan activé: {recruiter.email} → {plan}")
    except Exception as e:
        await db.rollback()
        print(f"Recruiter IPN: erreur activation {recruiter_id} — {e}")
        return {"status": "activation_error"}

    # Notification WhatsApp si phone disponible
    if recruiter.phone:
        try:
            await whatsapp_sender.send_text(
                recruiter.phone,
                f"🎉 *{recruiter.nom}*, votre abonnement *Recruteur {plan.title()}* est activé !\n\n"
                f"Vous pouvez maintenant publier vos annonces sur Prepa.\n"
                f"Connectez-vous sur votre dashboard pour commencer. 💼"
            )
        except Exception as e:
            print(f"Recruiter IPN WhatsApp: {e}")

    return {"status": "ok"}


@router.post("/payments/offreur-ipn")
async def offreur_paydunya_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Callback PayDunya après paiement offreur (contact_unlock ou offreur_plan)."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "invalid_payload"}

    if data.get("status", "").lower() != "completed":
        return {"status": "ignored"}

    custom = data.get("custom_data", {}) or {}
    payment_type = custom.get("type")
    phone = custom.get("phone")
    token = data.get("token", "")

    if not phone or not token:
        return {"status": "missing_data"}

    result = await db.execute(select(User).where(User.phone_number == phone))
    offreur = result.scalar_one_or_none()
    if not offreur:
        return {"status": "user_not_found"}

    from app.services.petit_job_service import petit_job_service
    from app.services.whatsapp.messages import messages

    try:
        if payment_type == "contact_unlock":
            import uuid as _uuid
            interest_id_str = custom.get("interest_id")
            if not interest_id_str:
                return {"status": "missing_interest_id"}
            interest_id = _uuid.UUID(interest_id_str)
            interest, candidat, job = await petit_job_service.unlock_contact(
                db, interest_id, paydunya_token=token
            )
            if not interest or not candidat or not job:
                return {"status": "interest_not_found"}
            await db.commit()
            # Envoie le numéro au offreur
            await whatsapp_sender.send_text(
                phone,
                messages.offreur_contact_unlocked(candidat.name, candidat.phone_number, job.titre)
            )

        elif payment_type == "offreur_plan":
            from datetime import datetime, timezone, timedelta
            offreur.offreur_plan_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            # Débloquer tous les contacts intéressés en attente
            unlocked = await petit_job_service.unlock_all_for_offreur(db, offreur, paydunya_token=token)
            await db.commit()
            await whatsapp_sender.send_text(
                phone,
                messages.offreur_plan_activated(len(unlocked))
            )
            # Envoyer les numéros déjà débloqués
            for candidat, job in unlocked:
                try:
                    await whatsapp_sender.send_text(
                        phone,
                        messages.offreur_contact_unlocked(candidat.name, candidat.phone_number, job.titre)
                    )
                except Exception:
                    pass
        else:
            return {"status": "unknown_type"}

    except Exception as e:
        await db.rollback()
        print(f"Offreur IPN erreur: {e}")
        return {"status": "error"}

    return {"status": "ok"}


@router.post("/payments/create")
async def create_payment(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Crée un lien de paiement PayDunya pour un élève."""
    data = await request.json()
    phone = data.get("phone")

    result = await db.execute(
        select(User).where(User.phone_number == phone)
    )
    user = result.scalar_one_or_none()

    if not user:
        return {"error": "user_not_found"}

    invoice = await payment_service.create_invoice(user=user, plan="pro")

    if invoice["success"]:
        await whatsapp_sender.send_text(
            phone,
            f"💳 Voici ton lien de paiement Prepa Pro :\n\n"
            f"{invoice['payment_url']}\n\n"
            f"Paiement sécurisé via Wave, Orange Money ou Free Money 🔒"
        )
        return {"success": True, "payment_url": invoice["payment_url"]}

    return {"success": False, "error": invoice.get("error")}