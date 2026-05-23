"""
Service de recherche sémantique dans la base vectorielle.
Supporte filtres granulaires : exam_type, series, matiere, chapitre.
Stratégie : recherche stricte d'abord, fallback élargi si pas de résultats.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
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
        chapitre: str = None,
        top_k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict]:
        """
        Recherche les chunks les plus pertinents.
        Stratégie en cascade :
        1. Recherche stricte (tous les filtres)
        2. Si < 3 résultats → élargit sans chapitre
        3. Si encore < 3 → élargit sans matière
        """
        query_embedding = await embedding_service.embed_text(query)
        if not query_embedding:
            return []

        # Tentative 1 — filtres complets
        results = await self._search_with_filters(
            db, query_embedding,
            exam_type=exam_type,
            series=series,
            subject=subject,
            chapitre=chapitre,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        if len(results) >= 3:
            return results

        # Tentative 2 — sans chapitre
        if chapitre:
            print(f"RAG: fallback sans chapitre (trouvé {len(results)})")
            results2 = await self._search_with_filters(
                db, query_embedding,
                exam_type=exam_type,
                series=series,
                subject=subject,
                chapitre=None,
                top_k=top_k,
                min_similarity=min_similarity,
            )
            if len(results2) > len(results):
                results = results2

        if len(results) >= 3:
            return results

        # Tentative 3 — sans matière ni chapitre
        if subject:
            print(f"RAG: fallback sans matiere (trouvé {len(results)})")
            results3 = await self._search_with_filters(
                db, query_embedding,
                exam_type=exam_type,
                series=series,
                subject=None,
                chapitre=None,
                top_k=top_k,
                min_similarity=min_similarity,
            )
            if len(results3) > len(results):
                results = results3

        return results

    async def _search_with_filters(
        self,
        db: AsyncSession,
        query_embedding: list[float],
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        chapitre: str = None,
        top_k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict]:
        """Recherche avec filtres spécifiques."""
        filters = [DocumentChunk.embedding.isnot(None)]

        if exam_type:
            filters.append(DocumentChunk.exam_type == exam_type)
        if series:
            # Cherche la série exacte OU null (documents transversaux)
            filters.append(
                or_(DocumentChunk.serie == series, DocumentChunk.serie.is_(None))
            )
        if subject:
            filters.append(DocumentChunk.matiere == subject)
        if chapitre:
            filters.append(DocumentChunk.chapitre == chapitre)

        stmt = select(DocumentChunk).where(and_(*filters))
        result = await db.execute(stmt)
        chunks = result.scalars().all()

        if not chunks:
            return []

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
                    "serie": chunk.serie,
                    "matiere": chunk.matiere,
                    "chapitre": chunk.chapitre,
                    "doc_type": chunk.doc_type,
                    "annee": chunk.annee,
                    "document_id": str(chunk.document_id),
                    "chunk_index": chunk.chunk_index,
                })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    async def build_context(
        self,
        db: AsyncSession,
        query: str,
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        chapitre: str = None,
        top_k: int = 5,
    ) -> str:
        """Construit le contexte RAG à injecter dans le prompt LLM."""
        results = await self.search(
            db=db,
            query=query,
            exam_type=exam_type,
            series=series,
            subject=subject,
            chapitre=chapitre,
            top_k=top_k,
        )

        if not results:
            return ""

        context_parts = []
        for i, r in enumerate(results, 1):
            # Métadonnées du chunk
            meta = []
            if r.get("matiere"):
                meta.append(r["matiere"])
            if r.get("chapitre"):
                meta.append(r["chapitre"].replace("_", " ").title())
            if r.get("doc_type"):
                meta.append(r["doc_type"])
            if r.get("annee"):
                meta.append(str(r["annee"]))

            meta_str = " · ".join(meta) if meta else "Programme officiel"
            context_parts.append(
                f"[Source {i} — {meta_str} — similarité {r['similarity']:.2f}]\n{r['content']}"
            )

        return "\n\n---\n\n".join(context_parts)


search_service = SearchService()