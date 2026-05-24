"""
Service de recherche hybride — sémantique + BM25.
Cascade de fallback si pas assez de résultats.
"""
import re
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
        Recherche hybride : sémantique + BM25.
        Cascade : strict → sans chapitre → sans matière.
        """
        query_embedding = await embedding_service.embed_text(query)
        if not query_embedding:
            return []

        # Tentative 1 — filtres complets
        results = await self._search_with_filters(
            db, query_embedding, query,
            exam_type=exam_type, series=series,
            subject=subject, chapitre=chapitre,
            top_k=top_k, min_similarity=min_similarity,
        )
        if len(results) >= 3:
            return results

        # Tentative 2 — sans chapitre
        if chapitre:
            print(f"RAG: fallback sans chapitre (trouvé {len(results)})")
            results2 = await self._search_with_filters(
                db, query_embedding, query,
                exam_type=exam_type, series=series,
                subject=subject, chapitre=None,
                top_k=top_k, min_similarity=min_similarity,
            )
            if len(results2) > len(results):
                results = results2

        if len(results) >= 3:
            return results

        # Tentative 3 — sans matière ni chapitre
        if subject:
            print(f"RAG: fallback sans matiere (trouvé {len(results)})")
            results3 = await self._search_with_filters(
                db, query_embedding, query,
                exam_type=exam_type, series=series,
                subject=None, chapitre=None,
                top_k=top_k, min_similarity=min_similarity,
            )
            if len(results3) > len(results):
                results = results3

        return results

    async def _search_with_filters(
        self,
        db: AsyncSession,
        query_embedding: list[float],
        query_text: str,
        exam_type: str = None,
        series: str = None,
        subject: str = None,
        chapitre: str = None,
        top_k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict]:
        """Recherche hybride sémantique + BM25 avec filtres."""
        filters = [DocumentChunk.embedding.isnot(None)]

        if exam_type:
            filters.append(DocumentChunk.exam_type == exam_type)
        if series:
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

        # Score sémantique
        semantic_scores = {}
        for chunk in chunks:
            if not chunk.embedding:
                continue
            sim = embedding_service.cosine_similarity(query_embedding, chunk.embedding)
            semantic_scores[chunk.id] = sim

        # Score BM25
        bm25_scores = self._bm25_score(query_text, chunks)

        # Score hybride combiné
        scored = []
        for chunk in chunks:
            sem = semantic_scores.get(chunk.id, 0)
            bm25 = bm25_scores.get(chunk.id, 0)

            # Pondération : 70% sémantique + 30% BM25
            hybrid_score = 0.7 * sem + 0.3 * bm25

            if sem >= min_similarity or (bm25 > 0.5 and sem >= 0.3):
                scored.append({
                    "content": chunk.content,
                    "similarity": hybrid_score,
                    "semantic_score": sem,
                    "bm25_score": bm25,
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

    def _bm25_score(self, query: str, chunks: list) -> dict:
        """Calcule les scores BM25 pour chaque chunk."""
        try:
            from rank_bm25 import BM25Okapi

            # Tokenise
            def tokenize(text: str) -> list[str]:
                text = text.lower()
                text = re.sub(r'[^\w\s]', ' ', text)
                return [t for t in text.split() if len(t) > 2]

            corpus = [tokenize(c.content) for c in chunks]
            query_tokens = tokenize(query)

            if not query_tokens or not any(corpus):
                return {}

            bm25 = BM25Okapi(corpus)
            scores = bm25.get_scores(query_tokens)

            # Normalise entre 0 et 1
            max_score = max(scores) if max(scores) > 0 else 1
            normalized = scores / max_score

            return {
                chunk.id: float(normalized[i])
                for i, chunk in enumerate(chunks)
            }
        except Exception as e:
            print(f"BM25 error: {e}")
            return {}

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
                f"[Source {i} — {meta_str} — score {r['similarity']:.2f}]\n{r['content']}"
            )

        return "\n\n---\n\n".join(context_parts)


search_service = SearchService()