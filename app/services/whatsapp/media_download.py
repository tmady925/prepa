"""
Téléchargement des médias entrants WhatsApp via l'endpoint Wasender.
====================================================================

WhatsApp chiffre les médias. Plutôt que de déchiffrer nous-mêmes (HKDF/AES),
on utilise l'endpoint officiel Wasender `POST /decrypt-media` : on lui passe le
message entrant, il renvoie une URL publique temporaire (~1h) vers le fichier
DÉCHIFFRÉ, qu'on télécharge ensuite par un simple GET.

API :
  await download_media(message) -> {"bytes": bytes, "filename": str} | None

`message` = l'objet message entrant COMPLET (avec ses clés "key" et "message").
Tolérant : toute erreur → None ; l'appelant gère le fallback honnête.
"""

import httpx

from app.core.settings import get_settings

settings = get_settings()

_MEDIA_KEYS = ("documentMessage", "imageMessage", "videoMessage", "audioMessage")


def _media_node(raw_message: dict) -> tuple[str, dict] | None:
    if not isinstance(raw_message, dict):
        return None
    for k in _MEDIA_KEYS:
        node = raw_message.get(k)
        if isinstance(node, dict):
            return k, node
    return None


def _filename(raw_message: dict) -> str:
    found = _media_node(raw_message)
    if not found:
        return "cv.pdf"
    key, node = found
    name = node.get("fileName") or node.get("filename")
    if name:
        return name
    return "cv.pdf" if key == "documentMessage" else "cv.jpg"


async def download_media(message: dict) -> dict | None:
    """
    Déchiffre (via Wasender) puis télécharge le média d'un message entrant.
    Retourne {"bytes", "filename"} ou None si pas de média / échec.
    """
    raw = (message or {}).get("message")
    if not _media_node(raw):
        return None

    # Wasender attend la même enveloppe que le webhook : data.messages{key, message}
    body = {
        "data": {
            "messages": {
                "key": (message or {}).get("key") or {},
                "message": raw,
            }
        }
    }
    decrypt_url = f"{settings.wasender_base_url}/decrypt-media"
    headers = {
        "Authorization": f"Bearer {settings.wasender_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(decrypt_url, headers=headers, json=body)
            if resp.status_code != 200:
                print(f"  [media_download] decrypt HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            public_url = (resp.json() or {}).get("publicUrl")
            if not public_url:
                print(f"  [media_download] pas de publicUrl: {resp.text[:200]}")
                return None

            f = await client.get(public_url)
            if f.status_code != 200 or not f.content:
                print(f"  [media_download] download HTTP {f.status_code}")
                return None
            return {"bytes": f.content, "filename": _filename(raw)}
    except Exception as e:
        print(f"  [media_download] erreur: {e}")
        return None
