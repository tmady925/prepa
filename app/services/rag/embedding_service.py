"""
Service d'embedding double-provider pour le RAG.

Architecture :
  JSONB      ← bge-m3 local (gratuit, toujours disponible)
  pgvector   ← Mistral embed (premium, meilleur français)

Indexation :
  chunk.embedding        = bge-m3  (toujours)
  chunk.embedding_vector = Mistral (si disponible)

Recherche :
  pgvector disponible → embed requête avec Mistral → cherche pgvector
  pgvector indispo    → embed requête avec bge-m3  → cherche JSONB

Cohérence garantie : chaque colonne utilise toujours son provider.
"""
import asyncio
import httpx
from app.core.settings import get_settings

settings = get_settings()

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False

EMBEDDING_DIM = 1024


class EmbeddingService:

    def __init__(self):
        self._pgvector_checked = False
        self._pgvector_enabled = False
        self._local_model = None

    # ── Check pgvector ────────────────────────────────────────────────

    async def check_pgvector(self, db) -> bool:
        """Vérifie pgvector via connexion indépendante."""
        if self._pgvector_checked:
            return self._pgvector_enabled
        try:
            from sqlalchemy import text
            async with db.bind.connect() as conn:
                await conn.execute(text("SELECT '[1,2,3]'::vector"))
            self._pgvector_enabled = True
            print("pgvector: disponible ✅")
        except Exception:
            self._pgvector_enabled = False
            print("pgvector: non disponible — fallback JSONB")
        self._pgvector_checked = True
        return self._pgvector_enabled

    # ── API publique — embed_text ─────────────────────────────────────

    async def embed_text(self, text: str) -> list[float] | None:
        """
        Embed pour les REQUÊTES de recherche.
        Utilisé dans search_service et detector.
        Retourne Mistral si disponible, sinon bge-m3.
        → Appelant doit savoir quel provider a été utilisé
          pour chercher dans la bonne colonne.
        """
        emb = await self.embed_text_mistral(text)
        if emb:
            return emb
        return await self.embed_text_local(text)

    async def embed_text_mistral(self, text: str) -> list[float] | None:
        """Embed Mistral — pour requêtes pgvector."""
        return await self._mistral_embed(text)

    async def embed_text_local(self, text: str) -> list[float]:
        """Embed bge-m3 — pour requêtes JSONB."""
        return await self._local_embed(text)

    # ── API publique — embed_batch ────────────────────────────────────

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Batch pour JSONB uniquement (bge-m3).
        Utilisé dans indexing_service pour remplir embedding (JSONB).
        Toujours disponible, jamais de rate limit.
        """
        return await self._local_embed_batch(texts)

    async def embed_batch_mistral(self, texts: list[str]) -> list[list[float] | None]:
        """
        Batch Mistral — pour remplir embedding_vector (pgvector).
        Peut retourner None pour certains chunks si rate limit.
        """
        results = await self._mistral_embed_batch(texts)
        if results and len(results) == len(texts):
            return results
        # Rate limit — retourne None pour tous
        print(f"Mistral batch indisponible — pgvector non rempli pour ce document")
        return [None] * len(texts)

    # ── Mistral ───────────────────────────────────────────────────────

    async def _mistral_embed(self, text: str) -> list[float] | None:
        if not settings.mistral_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={"model": "mistral-embed", "input": [text]},
                )
                data = response.json()
                if "data" not in data:
                    return None
                emb = data["data"][0]["embedding"]
                if len(emb) != EMBEDDING_DIM:
                    return None
                return emb
        except Exception as e:
            print(f"Mistral embed error: {e}")
            return None

    async def _mistral_embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Batch Mistral avec retry anti-rate-limit."""
        if not settings.mistral_api_key:
            return None

        all_embeddings = []
        batch_size = 10
        max_retries = 3
        total_batches = (len(texts) - 1) // batch_size + 1

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                for i in range(0, len(texts), batch_size):
                    batch = texts[i:i + batch_size]
                    batch_num = i // batch_size + 1
                    success = False

                    for attempt in range(max_retries):
                        try:
                            response = await client.post(
                                "https://api.mistral.ai/v1/embeddings",
                                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                                json={"model": "mistral-embed", "input": batch},
                            )
                            data = response.json()

                            if "data" not in data:
                                error_code = data.get("code", "")
                                if error_code == "3505" or response.status_code == 429:
                                    wait = 5 * (attempt + 1)
                                    print(f"  Rate limit batch {batch_num}/{total_batches} — attente {wait}s...")
                                    await asyncio.sleep(wait)
                                    continue
                                print(f"  Mistral batch erreur: {data}")
                                return None

                            batch_embeddings = [item["embedding"] for item in data["data"]]
                            all_embeddings.extend(batch_embeddings)
                            print(f"  → Mistral batch {batch_num}/{total_batches} ✅")
                            success = True
                            break

                        except Exception as e:
                            print(f"  Batch {batch_num} attempt {attempt+1} error: {e}")
                            await asyncio.sleep(2)

                    if not success:
                        print(f"  Batch {batch_num} échoué")
                        return None

                    if i + batch_size < len(texts):
                        await asyncio.sleep(1.0)

            return all_embeddings

        except Exception as e:
            print(f"Mistral batch embed error: {e}")
            return None

    # ── bge-m3 local ──────────────────────────────────────────────────

    async def _local_embed(self, text: str) -> list[float]:
        """Embed bge-m3 local — 1024 dims, gratuit."""
        try:
            model = self._get_local_model()
            if model is None:
                return [0.0] * EMBEDDING_DIM
            embedding = model.encode(
                text,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            result = embedding.tolist()
            return self._ensure_dim(result)
        except Exception as e:
            print(f"Local embed error: {e}")
            return [0.0] * EMBEDDING_DIM

    async def _local_embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch bge-m3 local — toujours disponible."""
        try:
            model = self._get_local_model()
            if model is None:
                return [[0.0] * EMBEDDING_DIM for _ in texts]

            print(f"  → bge-m3 local batch ({len(texts)} textes)...")
            embeddings = model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=True,
                batch_size=32,
            )
            results = []
            for emb in embeddings:
                results.append(self._ensure_dim(emb.tolist()))
            print(f"  → bge-m3 batch terminé ✅")
            return results

        except Exception as e:
            print(f"Local batch embed error: {e}")
            return [[0.0] * EMBEDDING_DIM for _ in texts]

    def _get_local_model(self):
        """Charge bge-m3 une seule fois en mémoire."""
        if self._local_model is not None:
            return self._local_model
        try:
            from sentence_transformers import SentenceTransformer
            print("Chargement BAAI/bge-m3 (1024 dims)...")
            self._local_model = SentenceTransformer("BAAI/bge-m3")
            print("bge-m3 chargé ✅")
            return self._local_model
        except Exception as e:
            print(f"Erreur chargement bge-m3: {e}")
            return None

    def _ensure_dim(self, result: list[float]) -> list[float]:
        """Garantit 1024 dimensions."""
        if len(result) == EMBEDDING_DIM:
            return result
        if len(result) < EMBEDDING_DIM:
            return result + [0.0] * (EMBEDDING_DIM - len(result))
        return result[:EMBEDDING_DIM]

    # ── Similarité cosinus (JSONB) ────────────────────────────────────

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Similarité cosinus — mode JSONB uniquement."""
        import math
        if len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


embedding_service = EmbeddingService()