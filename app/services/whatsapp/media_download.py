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

# Endpoint de déchiffrement documenté (host www, pas api).
_DECRYPT_URL = "https://www.wasenderapi.com/api/decrypt-media"

# Dernière raison d'échec (diagnostic temporaire, lisible côté chat).
_LAST_ERROR = ""


def get_last_error() -> str:
    return _LAST_ERROR


def _fail(reason: str):
    global _LAST_ERROR
    _LAST_ERROR = reason
    print(f"  [media_download] {reason}")
    return None


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
    global _LAST_ERROR
    _LAST_ERROR = ""
    raw = (message or {}).get("message")
    if not _media_node(raw):
        return _fail("aucun media dans le message")

    # Wasender attend la même enveloppe que le webhook : data.messages{key, message}
    body = {
        "data": {
            "messages": {
                "key": (message or {}).get("key") or {},
                "message": raw,
            }
        }
    }
    headers = {
        "Authorization": f"Bearer {settings.wasender_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(_DECRYPT_URL, headers=headers, json=body)
            if resp.status_code != 200:
                return _fail(f"decrypt HTTP {resp.status_code}: {resp.text[:120]}")
            data = resp.json() or {}
            public_url = data.get("publicUrl") or data.get("url") or (data.get("data") or {}).get("publicUrl")
            if not public_url:
                return _fail(f"pas de publicUrl: {str(data)[:120]}")

            f = await client.get(public_url)
            if f.status_code != 200 or not f.content:
                return _fail(f"download HTTP {f.status_code}")
            return {"bytes": f.content, "filename": _filename(raw)}
    except Exception as e:
        return _fail(f"exception {type(e).__name__}: {str(e)[:120]}")
