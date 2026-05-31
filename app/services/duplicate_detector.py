"""
Détection de doublons par hash SHA-256.
"""
import hashlib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def compute_hash(file_bytes: bytes) -> str:
    """Calcule le hash SHA-256 d'un fichier."""
    return hashlib.sha256(file_bytes).hexdigest()


async def check_duplicate_document(db: AsyncSession, file_hash: str) -> dict | None:
    """
    Vérifie si un document avec ce hash existe déjà.
    Retourne les infos du document existant ou None.
    """
    from app.models.document import Document
    result = await db.execute(
        select(Document).where(Document.file_hash == file_hash)
    )
    doc = result.scalar_one_or_none()
    if doc:
        return {
            "id": str(doc.id),
            "title": doc.title,
            "filename": doc.filename,
            "matiere": doc.matiere,
            "doc_type": doc.doc_type,
            "created_at": doc.created_at.isoformat(),
        }
    return None


async def check_duplicate_exercise(db: AsyncSession, file_hash: str) -> dict | None:
    """
    Vérifie si un exercice avec ce hash existe déjà.
    Retourne les infos de l'exercice existant ou None.
    """
    from app.models.exercise import Exercise
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
        }
    return None
