import base64
import httpx
from app.core.settings import get_settings

settings = get_settings()

WHATSAPP_API_URL = f"{settings.whatsapp_base_url}/messages"

HEADERS = {
    "Content-Type": "application/json",
    "D360-API-KEY": settings.whatsapp_api_key,
}


class WhatsAppSender:

    async def send_text(self, phone: str, text: str) -> dict:
        """Envoie un message texte simple."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        }
        return await self._send(payload)

    async def send_image_bytes(self, phone: str, image_bytes: bytes, caption: str = "") -> dict:
        """Envoie une image en base64 via WhatsApp."""
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "image",
            "image": {
                "data": image_b64,
                "mime_type": "image/png",
                "caption": caption,
            },
        }
        return await self._send(payload)

    async def send_image_url(self, phone: str, url: str, caption: str = "") -> dict:
        """Envoie une image via URL publique."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "image",
            "image": {
                "link": url,
                "caption": caption,
            },
        }
        return await self._send(payload)

    async def send_buttons(self, phone: str, text: str, buttons: list[dict]) -> dict:
        """
        Envoie un message avec boutons de réponse rapide.
        buttons = [{"id": "btn_1", "title": "BAC"}, ...]
        Max 3 boutons.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": btn["id"],
                                "title": btn["title"][:20],
                            },
                        }
                        for btn in buttons[:3]
                    ]
                },
            },
        }
        return await self._send(payload)

    async def send_list(self, phone: str, text: str, button_label: str, sections: list[dict]) -> dict:
        """
        Envoie un message avec liste de choix.
        sections = [{"title": "Examens", "rows": [{"id": "bac", "title": "BAC", "description": "..."}]}]
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": text},
                "action": {
                    "button": button_label,
                    "sections": sections,
                },
            },
        }
        return await self._send(payload)

    async def send_template(self, phone: str, template_name: str, language: str = "fr", components: list = None) -> dict:
        """Envoie un template HSM — utilisé pour les relances hors fenêtre 24h."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components or [],
            },
        }
        return await self._send(payload)

    async def _send(self, payload: dict) -> dict:
        """Envoie la requête HTTP à 360dialog."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    WHATSAPP_API_URL,
                    json=payload,
                    headers=HEADERS,
                )
                result = response.json()

                if response.status_code not in (200, 201):
                    print(f"WhatsApp API error {response.status_code}: {result}")

                return result

            except httpx.TimeoutException:
                print(f"WhatsApp API timeout pour {payload.get('to')}")
                return {"error": "timeout"}

            except Exception as e:
                print(f"WhatsApp API exception: {e}")
                return {"error": str(e)}


whatsapp_sender = WhatsAppSender()