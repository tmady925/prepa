"""
Service de recherche sémantique dans la base vectorielle.
"""
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.models.document import DocumentChunk
from app.services.rag.embedding_service import embedding_service


class SearchService:

    async def search(
        self,
        db: AsyncSession,
        query: str,
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        top_k: int = 5,
        min_similarity: float = 0.5,
    ) -> list[dict]:
        """
        Recherche les chunks les plus pertinents pour une requête.
        Retourne les top_k chunks triés par similarité.
        """
        # Génère l'embedding de la requête
        query_embedding = await embedding_service.embed_text(query)
        if not query_embedding:
            return []

        # Filtre par namespace
        filters = []
        if exam_type:
            filters.append(DocumentChunk.exam_type == exam_type)
        if series:
            filters.append(DocumentChunk.series == series)
        if subject:
            filters.append(DocumentChunk.subject == subject)

        # Récupère les chunks filtrés
        stmt = select(DocumentChunk).where(
            DocumentChunk.embedding.isnot(None)
        )
        if filters:
            stmt = stmt.where(and_(*filters))

        result = await db.execute(stmt)
        chunks = result.scalars().all()

        if not chunks:
            return []

        # Calcule la similarité cosinus pour chaque chunk
        scored = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            similarity = embedding_service.cosine_similarity(
                query_embedding, chunk.embedding
            )
            if similarity >= min_similarity:
                scored.append({
                    "content": chunk.content,
                    "similarity": similarity,
                    "exam_type": chunk.exam_type,
                    "series": chunk.series,
                    "subject": chunk.subject,
                    "document_id": str(chunk.document_id),
                    "chunk_index": chunk.chunk_index,
                })

        # Trie par similarité décroissante
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    async def build_context(
        self,
        db: AsyncSession,
        query: str,
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        top_k: int = 3,
    ) -> str:
        """
        Construit le contexte RAG à injecter dans le prompt LLM.
        """
        results = await self.search(
            db=db,
            query=query,
            exam_type=exam_type,
            series=series,
            subject=subject,
            top_k=top_k,
        )

        if not results:
            return ""

        context_parts = []
        for i, r in enumerate(results, 1):
            context_parts.append(
                f"[Extrait {i} — similarité {r['similarity']:.2f}]\n{r['content']}"
            )

        return "\n\n---\n\n".join(context_parts)


search_service = SearchService()