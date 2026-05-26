import re
import httpx
from app.core.settings import get_settings

settings = get_settings()


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.wasender_api_key}",
    }


class WhatsAppSender:

    def _clean_for_whatsapp(self, text: str) -> str:
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'\\\(\s*(.+?)\s*\\\)', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\\\[\s*(.+?)\s*\\\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def send_text(self, phone: str, text: str) -> dict:
        cleaned = self._clean_for_whatsapp(text)
        payload = {"phone": phone, "message": cleaned}
        return await self._send(payload)

    async def send_image_url(self, phone: str, url: str, caption: str = "") -> dict:
        payload = {"phone": phone, "image_url": url, "caption": caption}
        return await self._send(payload, endpoint="send-image")

    async def send_image_bytes(self, phone: str, image_bytes: bytes, caption: str = "") -> dict:
        print("send_image_bytes: utilise send_image_url avec une URL Cloudinary plutôt.")
        return {"error": "use_send_image_url"}

    async def send_buttons(self, phone: str, text: str, buttons: list[dict]) -> dict:
        cleaned = self._clean_for_whatsapp(text)
        payload = {
            "phone": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": cleaned},
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
        result = await self._send(payload)

        if not result.get("success", True) or result.get("error"):
            options = "\n".join(
                f"{i + 1}. {btn['title']}" for i, btn in enumerate(buttons[:3])
            )
            fallback_payload = {
                "phone": phone,
                "message": f"{cleaned}\n\n{options}",
            }
            return await self._send(fallback_payload)

        return result

    async def send_list(self, phone: str, text: str, button_label: str, sections: list[dict]) -> dict:
        cleaned = self._clean_for_whatsapp(text)
        payload = {
            "phone": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": cleaned},
                "action": {
                    "button": button_label,
                    "sections": sections,
                },
            },
        }
        result = await self._send(payload)

        if not result.get("success", True) or result.get("error"):
            lines = [cleaned, ""]
            idx = 1
            for section in sections:
                lines.append(f"*{section.get('title', '')}*")
                for row in section.get("rows", []):
                    lines.append(f"{idx}. {row['title']}")
                    idx += 1
                lines.append("")
            fallback_payload = {
                "phone": phone,
                "message": "\n".join(lines).strip(),
            }
            return await self._send(fallback_payload)

        return result

    async def send_template(self, phone: str, template_name: str, language: str = "fr", components: list = None) -> dict:
        payload = {
            "phone": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components or [],
            },
        }
        return await self._send(payload)

    async def _send(self, payload: dict, endpoint: str = "send-message") -> dict:
        """Envoie la requête HTTP à Wasender."""

        # Mode développement — simule l'envoi sans appeler Wasender
        if settings.app_env == "development":
            msg_preview = payload.get("message", payload.get("image_url", str(payload)))[:80]
            print(f"[DEV] WhatsApp → {payload.get('phone')}: {msg_preview}")
            return {"success": True, "dev_mode": True}

        url = f"{settings.wasender_base_url}/{endpoint}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers=_headers(),
                )
                result = response.json()

                if response.status_code not in (200, 201):
                    print(f"Wasender API error {response.status_code}: {result}")

                return result

            except httpx.TimeoutException:
                print(f"Wasender API timeout pour {payload.get('phone')}")
                return {"error": "timeout"}

            except Exception as e:
                print(f"Wasender API exception: {e}")
                return {"error": str(e)}


whatsapp_sender = WhatsAppSender()