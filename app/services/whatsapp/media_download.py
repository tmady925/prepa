"""
Téléchargement + déchiffrement des médias entrants WhatsApp (via Wasender).
============================================================================

WhatsApp chiffre les médias. Le webhook fournit, dans documentMessage / imageMessage :
  url, mediaKey, mimetype, fileName, fileLength.
Il n'existe PAS d'endpoint Wasender qui renvoie le fichier déchiffré — on déchiffre
nous-mêmes (schéma standard WhatsApp : HKDF-SHA256 → AES-256-CBC, MAC sur 10 octets).

API :
  await download_media(raw_message) -> {"bytes", "filename", "mimetype"} | None

Tolérant : toute erreur (réseau, clé, format) → None. L'appelant gère le fallback.
"""

import base64

import httpx

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


_MEDIA_INFO = {
    "image": b"WhatsApp Image Keys",
    "document": b"WhatsApp Document Keys",
    "video": b"WhatsApp Video Keys",
    "audio": b"WhatsApp Audio Keys",
}


def _b64decode(s: str) -> bytes:
    """Décode base64 standard ou url-safe, avec padding tolérant."""
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad)
    except Exception:
        return base64.urlsafe_b64decode(s + pad)


def _extract_media_node(raw_message: dict) -> tuple[str, dict] | None:
    """Retourne (media_type, node) depuis documentMessage/imageMessage/… ."""
    if not isinstance(raw_message, dict):
        return None
    mapping = {
        "documentMessage": "document",
        "imageMessage": "image",
        "videoMessage": "video",
        "audioMessage": "audio",
    }
    for key, mtype in mapping.items():
        node = raw_message.get(key)
        if isinstance(node, dict) and node.get("url") and node.get("mediaKey"):
            return mtype, node
    return None


def _decrypt(enc: bytes, media_key_b64: str, media_type: str, file_length: int | None) -> bytes:
    media_key = _b64decode(media_key_b64)
    expanded = HKDF(
        algorithm=hashes.SHA256(),
        length=112,
        salt=None,
        info=_MEDIA_INFO.get(media_type, _MEDIA_INFO["document"]),
    ).derive(media_key)

    iv = expanded[:16]
    cipher_key = expanded[16:48]

    ciphertext = enc[:-10] if len(enc) > 10 else enc  # retire le MAC (10 octets)
    decryptor = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()

    # Priorité à fileLength (exact) ; sinon retire le padding PKCS7.
    if file_length and 0 < file_length <= len(plain):
        return plain[:file_length]
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and len(plain) >= pad:
            return plain[:-pad]
    return plain


async def download_media(raw_message: dict) -> dict | None:
    """
    Télécharge et déchiffre le média d'un message entrant.
    Retourne {"bytes": bytes, "filename": str, "mimetype": str} ou None.
    """
    found = _extract_media_node(raw_message)
    if not found:
        return None
    media_type, node = found

    url = node.get("url")
    media_key = node.get("mediaKey")
    mimetype = node.get("mimetype") or ""
    filename = node.get("fileName") or node.get("filename") or f"cv.{_ext_from_mime(mimetype, media_type)}"
    try:
        file_length = int(node.get("fileLength")) if node.get("fileLength") else None
    except (TypeError, ValueError):
        file_length = None

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200 or not resp.content:
                print(f"  [media_download] HTTP {resp.status_code} sur {url[:60]}")
                return None
            enc = resp.content

        data = _decrypt(enc, media_key, media_type, file_length)
        if not data:
            return None
        return {"bytes": data, "filename": filename, "mimetype": mimetype}
    except Exception as e:
        print(f"  [media_download] erreur: {e}")
        return None


def _ext_from_mime(mimetype: str, media_type: str) -> str:
    m = (mimetype or "").lower()
    if "pdf" in m:
        return "pdf"
    if "png" in m:
        return "png"
    if "jpeg" in m or "jpg" in m:
        return "jpg"
    if "word" in m or "document" in m:
        return "docx"
    return "pdf" if media_type == "document" else "jpg"
