"""
Service d'indexation des documents dans la base vectorielle.
"""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.document import Document, DocumentChunk
from app.services.rag.document_processor import document_processor
from app.services.rag.embedding_service import embedding_service


class IndexingService:

    async def index_document(
        self,
        db: AsyncSession,
        file_bytes: bytes,
        filename: str,
        title: str,
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        doc_type: str = "cours",
        uploaded_by: str = "admin",
        chapitre: str = None,
        pays: str = "senegal",
        annee: int = None,
        niveau: int = 2,
        source: str = None,
    ) -> dict:
        """
        Indexe un document complet.
        1. Extrait le texte
        2. Découpe en chunks
        3. Génère les embeddings
        4. Sauvegarde en base
        5. Lance l'analyse cerveau automatiquement
        """
        ext = filename.lower().split(".")[-1]

        # Crée l'entrée document
        doc = Document(
            title=title,
            filename=filename,
            file_type=ext,
            pays=pays,
            exam_type=exam_type,
            serie=series,
            matiere=subject,
            chapitre=chapitre,
            doc_type=doc_type,
            annee=annee,
            niveau=niveau,
            source=source,
            status="processing",
            uploaded_by=uploaded_by,
        )
        db.add(doc)
        await db.flush()

        try:
            # Traitement du document
            print(f"Traitement de {filename}...")
            result = await document_processor.process(file_bytes, filename)

            if result.get("error"):
                doc.status = "error"
                doc.error_message = result["error"]
                await db.commit()
                return {"success": False, "error": result["error"]}

            chunks = result["chunks"]
            print(f"  → {len(chunks)} chunks extraits")

            if not chunks:
                doc.status = "error"
                doc.error_message = "Aucun texte extrait"
                await db.commit()
                return {"success": False, "error": "Aucun texte extrait"}

            # Génère les embeddings en batch
            print(f"  → Génération des embeddings...")
            embeddings = await embedding_service.embed_batch(chunks)
            print(f"  → {len(embeddings)} embeddings générés")

            # Sauvegarde les chunks avec métadonnées granulaires
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                if not embedding:
                    continue
                doc_chunk = DocumentChunk(
                    document_id=doc.id,
                    content=chunk,
                    chunk_index=i,
                    pays=pays,
                    exam_type=exam_type,
                    serie=series,
                    matiere=subject,
                    chapitre=chapitre,
                    doc_type=doc_type,
                    annee=annee,
                    niveau=niveau,
                    embedding=embedding,
                )
                db.add(doc_chunk)

            # Met à jour le document
            doc.status = "indexed"
            doc.page_count = result["page_count"]
            doc.chunk_count = len(chunks)
            doc.has_ocr = result["has_ocr"]

            await db.commit()
            print(f"  ✅ Document indexé : {len(chunks)} chunks")

            # Analyse automatique par le cerveau
            if subject and exam_type:
                try:
                    from app.services.rag.brain_service import brain_service
                    await brain_service.analyze_document(
                        db=db,
                        document_id=str(doc.id),
                    )
                    print(f"  🧠 Analyse cerveau terminée")
                except Exception as e:
                    print(f"  Brain analysis error: {e}")

            return {
                "success": True,
                "document_id": str(doc.id),
                "chunks": len(chunks),
                "pages": result["page_count"],
                "has_ocr": result["has_ocr"],
            }

        except Exception as e:
            doc.status = "error"
            doc.error_message = str(e)
            await db.commit()
            print(f"  ❌ Erreur indexation: {e}")
            return {"success": False, "error": str(e)}

    async def delete_document(self, db: AsyncSession, document_id: str) -> bool:
        """Supprime un document et tous ses chunks."""
        result = await db.execute(
            select(Document).where(Document.id == uuid.UUID(document_id))
        )
        doc = result.scalar_one_or_none()
        if not doc:
            return False
        await db.delete(doc)
        await db.commit()
        return True

    async def get_documents(self, db: AsyncSession) -> list[dict]:
        """Liste tous les documents indexés."""
        result = await db.execute(
            select(Document).order_by(Document.created_at.desc())
        )
        docs = result.scalars().all()
        return [
            {
                "id": str(d.id),
                "title": d.title,
                "filename": d.filename,
                "exam_type": d.exam_type,
                "serie": d.serie,
                "matiere": d.matiere,
                "chapitre": d.chapitre,
                "doc_type": d.doc_type,
                "annee": d.annee,
                "niveau": d.niveau,
                "status": d.status,
                "page_count": d.page_count,
                "chunk_count": d.chunk_count,
                "has_ocr": d.has_ocr,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ]


indexing_service = IndexingService()