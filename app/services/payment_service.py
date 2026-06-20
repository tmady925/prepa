import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.settings import get_settings
from app.models.subscription import Subscription
from app.models.user import User

settings = get_settings()

PAYDUNYA_BASE_URL = (
    "https://app.paydunya.com/api/v1"
    if settings.paydunya_mode == "live"
    else "https://app.paydunya.com/sandbox-api/v1"
)

def _paydunya_headers() -> dict:
    return {
        "PAYDUNYA-MASTER-KEY": settings.paydunya_master_key,
        "PAYDUNYA-PRIVATE-KEY": settings.paydunya_private_key,
        "PAYDUNYA-TOKEN": settings.paydunya_token,
        "Content-Type": "application/json",
    }


class PaymentService:

    async def create_invoice(
        self,
        user: User,
        plan: str = "pro",
        amount_fcfa: int = 500,
    ) -> dict:
        """Crée une facture PayDunya et retourne le lien de paiement."""

        payload = {
            "invoice": {
                "items": {
                    "item_0": {
                        "name": f"Prepa {plan.title()}",
                        "quantity": 1,
                        "unit_price": str(amount_fcfa),
                        "total_price": str(amount_fcfa),
                        "description": f"Abonnement Prepa {plan.title()} - 1 mois",
                    }
                },
                "taxes": {},
                "total_amount": str(amount_fcfa),
                "description": f"Prepa {plan.title()} pour {user.name or user.phone_number}",
            },
            "store": {
                "name": "Prepa",
                "tagline": "Ton assistant de revision",
                "phone": settings.whatsapp_number,
            },
            "actions": {
                "cancel_url": "https://prepa.app/cancel",
                "return_url": "https://prepa.app/success",
                "callback_url": f"{settings.app_base_url}/api/v1/payments/ipn",
            },
            "custom_data": {
                "user_id": str(user.id),
                "phone": user.phone_number,
                "plan": plan,
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    f"{PAYDUNYA_BASE_URL}/checkout-invoice/create",
                    json=payload,
                    headers=_paydunya_headers(),
                )
                print(f"PayDunya status: {response.status_code}")
                print(f"PayDunya response: {response.text[:300]}")

                if not response.text.strip():
                    return {"success": False, "error": "Reponse vide de PayDunya"}

                data = response.json()

            except httpx.TimeoutException:
                return {"success": False, "error": "Timeout PayDunya"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        if data.get("response_code") == "00":
            return {
                "success": True,
                "token": data.get("token"),
                "payment_url": data.get("response_text"),  # response_text contient l'URL de paiement
                "raw": data,
            }

        print(f"PayDunya error: {data}")
        return {"success": False, "error": data}

    async def activate_pro(
        self,
        db: AsyncSession,
        user: User,
        paydunya_token: str,
        payment_method: str = None,
        raw_data: dict = None,
    ) -> Subscription:
        """Active le plan Pro apres paiement confirme."""
        from app.services.config_service import config_service

        amount = await config_service.get_int("plan_pro_price_fcfa")
        now = datetime.now(ZoneInfo("Africa/Dakar"))

        subscription = Subscription(
            user_id=user.id,
            plan="pro",
            status="active",
            started_at=now,
            expires_at=now + timedelta(days=30),
            amount_fcfa=amount,
            payment_method=payment_method,
            paydunya_token=paydunya_token,
            paydunya_raw=raw_data,
        )
        db.add(subscription)

        user.plan = "pro"
        await db.flush()

        print(f"Pro active pour {user.phone_number}")
        return subscription

    async def create_offreur_invoice(
        self,
        offreur: User,
        payment_type: str,          # "contact_unlock" | "offreur_plan"
        interest_id: str | None,    # UUID de PetitJobInterest (pour contact_unlock)
        job_titre: str = "",
    ) -> dict:
        """
        Crée une facture PayDunya pour l'offreur.
        - contact_unlock : 500 FCFA, 1 contact
        - offreur_plan   : 2 000 FCFA, 30 jours illimité
        """
        if payment_type == "contact_unlock":
            amount = 500
            label = f"Contact candidat — {job_titre}"
            description = "Déblocage d'un contact candidat (1 numéro)"
        else:
            amount = 2000
            label = "Offreur Mensuel"
            description = "Abonnement mensuel — contacts candidats illimités (30j)"

        payload = {
            "invoice": {
                "items": {
                    "item_0": {
                        "name": label,
                        "quantity": 1,
                        "unit_price": str(amount),
                        "total_price": str(amount),
                        "description": description,
                    }
                },
                "taxes": {},
                "total_amount": str(amount),
                "description": f"{label} pour {offreur.name or offreur.phone_number}",
            },
            "store": {
                "name": "Prepa",
                "tagline": "Ton assistant emploi",
                "phone": settings.whatsapp_number,
            },
            "actions": {
                "cancel_url": "https://prepa.app/cancel",
                "return_url": "https://prepa.app/success",
                "callback_url": f"{settings.app_base_url}/api/v1/payments/offreur-ipn",
            },
            "custom_data": {
                "phone": offreur.phone_number,
                "type": payment_type,
                "interest_id": interest_id,
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    f"{PAYDUNYA_BASE_URL}/checkout-invoice/create",
                    json=payload,
                    headers=_paydunya_headers(),
                )
                if not response.text.strip():
                    return {"success": False, "error": "Réponse vide PayDunya"}
                data = response.json()
            except httpx.TimeoutException:
                return {"success": False, "error": "Timeout PayDunya"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        if data.get("response_code") == "00":
            return {
                "success": True,
                "token": data.get("token"),
                "payment_url": data.get("response_text"),
            }

        print(f"PayDunya offreur error: {data}")
        return {"success": False, "error": data}

    async def verify_payment(self, token: str) -> dict:
        """Verifie le statut d'un paiement PayDunya."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    f"{PAYDUNYA_BASE_URL}/softorder/details/{token}",
                    headers=_paydunya_headers(),
                )
                return response.json()
            except Exception as e:
                return {"error": str(e)}


payment_service = PaymentService()