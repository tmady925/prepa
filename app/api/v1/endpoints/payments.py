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