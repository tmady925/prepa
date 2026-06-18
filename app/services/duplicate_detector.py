"""
Détection de doublons par hash SHA-256.
- Niveau 1 : hash du fichier (doublon exact)
- Niveau 2 : hash du contenu texte normalisé (doublon de contenu)
"""
import hashlib
import re
import unicodedata
from sqlalchemy.ext.asyncio import AsyncSession


def compute_hash(file_bytes: bytes) -> str:
    """Hash SHA-256 du fichier brut."""
    return hashlib.sha256(file_bytes).hexdigest()


def normalize_text(text: str) -> str:
    """Normalise le texte : minuscules, sans accents, sans ponctuation, sans espaces multiples."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def compute_content_hash(file_bytes: bytes) -> str | None:
    """Extrait le texte d'un PDF et calcule son hash normalisé."""
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text("text")
        doc.close()
        text = normalize_text(text)
        if not text.strip():
            return None
        return hashlib.sha256(text.encode()).hexdigest()
    except Exception:
        return None


async def check_duplicate_document(
    db: AsyncSession,
    file_hash: str,
    content_hash: str | None = None,
) -> dict | None:
    """Désactivé — module documents supprimé (plateforme emploi uniquement)."""
    return None


async def check_duplicate_exercise(
    db: AsyncSession,
    file_hash: str,
    content_hash: str | None = None,
) -> dict | None:
    """Désactivé — module exercices supprimé (plateforme emploi uniquement)."""
    return None
