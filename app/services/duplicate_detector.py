"""
Détection de doublons par hash SHA-256.
- Niveau 1 : hash du fichier (doublon exact)
- Niveau 2 : hash du contenu texte normalisé (doublon de contenu)
"""
import hashlib
import re
import unicodedata
from sqlalchemy import select
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
    """Vérifie si un document existe déjà (fichier exact ou contenu identique)."""
    from app.models.document import Document

    # Niveau 1 — hash fichier exact
    result = await db.execute(
        select(Document).where(Document.file_hash == file_hash)
    )
    doc = result.scalar_one_or_none()
    if doc:
        return {
            "id": str(doc.id),
            "title": doc.title,
            "matiere": doc.matiere,
            "doc_type": doc.doc_type,
            "created_at": doc.created_at.isoformat(),
            "type": "exact",
        }

    # Niveau 2 — hash contenu texte
    if content_hash:
        result = await db.execute(
            select(Document).where(Document.content_hash == content_hash)
        )
        doc = result.scalar_one_or_none()
        if doc:
            return {
                "id": str(doc.id),
                "title": doc.title,
                "matiere": doc.matiere,
                "doc_type": doc.doc_type,
                "created_at": doc.created_at.isoformat(),
                "type": "content",
            }

    return None


async def check_duplicate_exercise(
    db: AsyncSession,
    file_hash: str,
    content_hash: str | None = None,
) -> dict | None:
    """Vérifie si un exercice existe déjà (fichier exact ou contenu identique)."""
    from app.models.exercise import Exercise

    # Niveau 1 — hash fichier exact
    result = await db.execute(
        select(Exercise).where(Exercise.file_hash == file_hash)
    )
    ex = result.scalar_one_or_none()
    if ex:
        return {
            "id": str(ex.id),
            "title": ex.title,
            "matiere": ex.matiere,
            "exam_type": ex.exam_type,
            "serie": ex.serie,
            "niveau": ex.niveau,
            "created_at": ex.created_at.isoformat(),
            "type": "exact",
        }

    # Niveau 2 — hash contenu texte
    if content_hash:
        result = await db.execute(
            select(Exercise).where(Exercise.content_hash == content_hash)
        )
        ex = result.scalar_one_or_none()
        if ex:
            return {
                "id": str(ex.id),
                "title": ex.title,
                "matiere": ex.matiere,
                "exam_type": ex.exam_type,
                "serie": ex.serie,
                "niveau": ex.niveau,
                "created_at": ex.created_at.isoformat(),
                "type": "content",
            }

    return None
