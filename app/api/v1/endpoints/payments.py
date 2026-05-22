from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import get_db
from app.models.user import User
from app.services.payment_service import payment_service
from app.services.whatsapp.sender import whatsapp_sender

router = APIRouter()


@router.post("/payments/webhook")
async def paydunya_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Reçoit la notification de PayDunya après paiement."""
    data = await request.json()
    print(f"PayDunya webhook: {data}")

    # Vérifie que le paiement est complété
    status = data.get("status", "")
    if status.lower() != "completed":
        return {"status": "ignored"}

    # Récupère les données custom
    custom = data.get("custom_data", {})
    phone = custom.get("phone")
    plan = custom.get("plan", "pro")
    token = data.get("token", "")
    payment_method = data.get("payment_method", "")

    if not phone:
        return {"status": "no_phone"}

    # Trouve l'utilisateur
    result = await db.execute(
        select(User).where(User.phone_number == phone)
    )
    user = result.scalar_one_or_none()

    if not user:
        return {"status": "user_not_found"}

    # Active le Pro
    await payment_service.activate_pro(
        db=db,
        user=user,
        paydunya_token=token,
        payment_method=payment_method,
        raw_data=data,
    )
    await db.commit()

    # Notifie l'élève sur WhatsApp
    await whatsapp_sender.send_text(
        phone,
        f"🎉 Félicitations *{user.name}* !\n\n"
        f"Ton abonnement *Prepa Pro* est activé pour 30 jours.\n\n"
        f"Tu peux maintenant réviser sans limite. Bonne chance ! 💪"
    )

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
        # Envoie le lien via WhatsApp
        await whatsapp_sender.send_text(
            phone,
            f"💳 Voici ton lien de paiement Prepa Pro :\n\n"
            f"{invoice['payment_url']}\n\n"
            f"Paiement sécurisé via Wave, Orange Money ou Free Money 🔒"
        )
        return {"success": True, "payment_url": invoice["payment_url"]}

    return {"success": False, "error": invoice.get("error")}