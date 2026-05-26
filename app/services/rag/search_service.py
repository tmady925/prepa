"""
Service de recherche hybride — sémantique + BM25.
Provider unique : Mistral embed pour toutes les colonnes.
  pgvector → embed requête avec Mistral (cohérence avec embedding_vector)
  JSONB    → embed requête avec Mistral (cohérence avec embedding)
Cascade de fallback si pas assez de résultats.
"""
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, text
from app.models.document import DocumentChunk
from app.services.rag.embedding_service import embedding_service


class SearchService:

    def __init__(self):
        self._pgvector_available = None

    async def _check_pgvector(self, db: AsyncSession) -> bool:
        """Vérifie pgvector une seule fois puis cache le résultat."""
        if self._pgvector_available is not None:
            return self._pgvector_available
        self._pgvector_available = await embedding_service.check_pgvector(db)
        return self._pgvector_available

    # ── Entrée principale ─────────────────────────────────────────────

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
        Embed la requête avec Mistral — même espace que les deux colonnes.
        Cascade : strict → sans chapitre → sans matière.
        """
        use_pgvector = await self._check_pgvector(db)

        # Embed la requête avec Mistral (provider unique)
        query_embedding = await embedding_service.embed_text_mistral(query)
        if not query_embedding:
            print("Mistral embed indisponible — recherche impossible")
            return []

        # Tentative 1 — filtres complets
        results = await self._search_with_filters(
            db, query_embedding, query,
            exam_type=exam_type, series=series,
            subject=subject, chapitre=chapitre,
            top_k=top_k, min_similarity=min_similarity,
            use_pgvector=use_pgvector,
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
                use_pgvector=use_pgvector,
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
                use_pgvector=use_pgvector,
            )
            if len(results3) > len(results):
                results = results3

        return results

    # ── Dispatch pgvector / JSONB ─────────────────────────────────────

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
        use_pgvector: bool = False,
    ) -> list[dict]:
        if use_pgvector:
            return await self._search_pgvector(
                db, query_embedding, query_text,
                exam_type, series, subject, chapitre,
                top_k, min_similarity,
            )
        return await self._search_jsonb(
            db, query_embedding, query_text,
            exam_type, series, subject, chapitre,
            top_k, min_similarity,
        )

    # ── Mode pgvector — SQL natif ─────────────────────────────────────

    async def _search_pgvector(
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
        """Recherche vectorielle native pgvector avec index HNSW."""
        conditions = ["embedding_vector IS NOT NULL"]
        params = {"query_vec": str(query_embedding), "limit": top_k * 3}

        if exam_type:
            conditions.append("exam_type = :exam_type")
            params["exam_type"] = exam_type
        if series:
            conditions.append("(serie = :series OR serie IS NULL)")
            params["series"] = series
        if subject:
            conditions.append("matiere = :subject")
            params["subject"] = subject
        if chapitre:
            conditions.append("chapitre = :chapitre")
            params["chapitre"] = chapitre

        where_clause = " AND ".join(conditions)
        params["min_sim"] = min_similarity

        sql = text(f"""
            SELECT
                id, content, chunk_index, exam_type, serie,
                matiere, chapitre, doc_type, annee, document_id,
                1 - (embedding_vector <=> CAST(:query_vec AS vector)) AS semantic_score
            FROM document_chunks
            WHERE {where_clause}
              AND 1 - (embedding_vector <=> CAST(:query_vec AS vector)) >= :min_sim
            ORDER BY embedding_vector <=> CAST(:query_vec AS vector)
            LIMIT :limit
        """)

        try:
            result = await db.execute(sql, params)
            rows = result.fetchall()
        except Exception as e:
            print(f"pgvector search error: {e} — fallback JSONB")
            self._pgvector_available = False
            try:
                await db.rollback()
            except Exception:
                pass
            return await self._search_jsonb(
                db, query_embedding, query_text,
                exam_type, series, subject, chapitre,
                top_k, min_similarity,
            )

        if not rows:
            return []

        # Récupère les chunks pour BM25
        chunks_result = await db.execute(
            select(DocumentChunk).where(
                DocumentChunk.id.in_([r.id for r in rows])
            )
        )
        chunks_by_id = {c.id: c for c in chunks_result.scalars().all()}
        chunks_list = [chunks_by_id[r.id] for r in rows if r.id in chunks_by_id]
        bm25_scores = self._bm25_score(query_text, chunks_list)

        scored = []
        for row in rows:
            sem = float(row.semantic_score)
            bm25 = bm25_scores.get(row.id, 0)
            hybrid = 0.8 * sem + 0.2 * bm25
            scored.append({
                "content": row.content,
                "similarity": hybrid,
                "semantic_score": sem,
                "bm25_score": bm25,
                "exam_type": row.exam_type,
                "serie": row.serie,
                "matiere": row.matiere,
                "chapitre": row.chapitre,
                "doc_type": row.doc_type,
                "annee": row.annee,
                "document_id": str(row.document_id),
                "chunk_index": row.chunk_index,
            })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        print(f"pgvector (Mistral): {len(scored)} résultats")
        return scored[:top_k]

    # ── Mode JSONB — cosinus Python ───────────────────────────────────

    async def _search_jsonb(
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
        """Recherche JSONB avec cosinus Python — Mistral embeddings."""
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

        semantic_scores = {}
        for chunk in chunks:
            if not chunk.embedding:
                continue
            sim = embedding_service.cosine_similarity(query_embedding, chunk.embedding)
            semantic_scores[chunk.id] = sim

        bm25_scores = self._bm25_score(query_text, chunks)

        scored = []
        for chunk in chunks:
            sem = semantic_scores.get(chunk.id, 0)
            bm25 = bm25_scores.get(chunk.id, 0)
            hybrid_score = 0.8 * sem + 0.2 * bm25

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
        print(f"JSONB (Mistral): {len(scored)} résultats")
        return scored[:top_k]

    # ── BM25 ──────────────────────────────────────────────────────────

    def _bm25_score(self, query: str, chunks: list) -> dict:
        """Calcule les scores BM25."""
        try:
            from rank_bm25 import BM25Okapi

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
            max_score = max(scores) if max(scores) > 0 else 1
            normalized = scores / max_score

            return {
                chunk.id: float(normalized[i])
                for i, chunk in enumerate(chunks)
            }
        except Exception as e:
            print(f"BM25 error: {e}")
            return {}

    # ── Build context ─────────────────────────────────────────────────

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
