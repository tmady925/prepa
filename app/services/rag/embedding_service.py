"""
Service d'embedding — provider unique : Mistral.

Architecture :
  JSONB      ← Mistral embed (stocké aussi en JSONB pour fallback)
  pgvector   ← Mistral embed (recherche vectorielle native)

Indexation :
  chunk.embedding        = Mistral  (toujours)
  chunk.embedding_vector = Mistral  (si pgvector disponible)

Recherche :
  pgvector disponible → embed requête avec Mistral → cherche pgvector
  pgvector indispo    → embed requête avec Mistral → cherche JSONB (cosinus Python)
"""
import math
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
        Embed pour les REQUÊTES de recherche — Mistral uniquement.
        Retourne None si Mistral indisponible.
        """
        return await self._mistral_embed(text)

    async def embed_text_mistral(self, text: str) -> list[float] | None:
        """Alias — embed Mistral pour requêtes pgvector."""
        return await self._mistral_embed(text)

    # ── API publique — embed_batch ────────────────────────────────────

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """
        Batch Mistral — pour remplir embedding (JSONB) et embedding_vector (pgvector).
        Peut retourner None pour certains chunks si rate limit.
        """
        results = await self._mistral_embed_batch(texts)
        if results and len(results) == len(texts):
            return results
        print(f"Mistral batch indisponible — chunks non indexés")
        return [None] * len(texts)

    async def embed_batch_mistral(self, texts: list[str]) -> list[list[float] | None]:
        """Alias — même comportement que embed_batch (Mistral uniquement)."""
        return await self.embed_batch(texts)

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

    # ── Similarité cosinus (fallback JSONB) ───────────────────────────

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Similarité cosinus — utilisée en mode JSONB."""
        if len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


embedding_service = EmbeddingService()
