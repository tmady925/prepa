"""
Service d'embedding pour le RAG.
Provider principal : Mistral embed (1024 dim)
Fallback : sentence-transformers local
Stockage : pgvector (natif) ou JSONB (fallback)
"""
import httpx
from app.core.settings import get_settings

settings = get_settings()

# Détecte pgvector
try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False


class EmbeddingService:

    def __init__(self):
        self._pgvector_checked = False
        self._pgvector_enabled = False

    # ── Check pgvector ────────────────────────────────────────────────

    async def check_pgvector(self, db) -> bool:
        """Vérifie si pgvector est disponible en base."""
        if self._pgvector_checked:
            return self._pgvector_enabled

        try:
            from sqlalchemy import text
            await db.execute(text("SELECT '[1,2,3]'::vector"))
            self._pgvector_enabled = True
            print("pgvector: disponible ✅")
        except Exception:
            self._pgvector_enabled = False
            print("pgvector: non disponible — fallback JSONB")

        self._pgvector_checked = True
        return self._pgvector_enabled

    # ── Embed ─────────────────────────────────────────────────────────

    async def embed_text(self, text: str) -> list[float] | None:
        """Génère l'embedding d'un texte."""
        embedding = await self._mistral_embed(text)
        if embedding:
            return embedding
        print("Mistral embed failed — fallback local")
        return await self._local_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Génère les embeddings d'une liste de textes."""
        embeddings = await self._mistral_embed_batch(texts)
        if embeddings:
            return embeddings
        results = []
        for text in texts:
            emb = await self._local_embed(text)
            results.append(emb or [])
        return results

    # ── Mistral ───────────────────────────────────────────────────────

    async def _mistral_embed(self, text: str) -> list[float] | None:
        if not settings.mistral_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={"model": "mistral-embed", "input": [text]},
                )
                data = response.json()
                return data["data"][0]["embedding"]
        except Exception as e:
            print(f"Mistral embed error: {e}")
            return None

    async def _mistral_embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        if not settings.mistral_api_key:
            return None
        try:
            all_embeddings = []
            batch_size = 32
            async with httpx.AsyncClient(timeout=60.0) as client:
                for i in range(0, len(texts), batch_size):
                    batch = texts[i:i + batch_size]
                    response = await client.post(
                        "https://api.mistral.ai/v1/embeddings",
                        headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                        json={"model": "mistral-embed", "input": batch},
                    )
                    data = response.json()
                    batch_embeddings = [item["embedding"] for item in data["data"]]
                    all_embeddings.extend(batch_embeddings)
            return all_embeddings
        except Exception as e:
            print(f"Mistral batch embed error: {e}")
            return None

    # ── Local fallback ────────────────────────────────────────────────

    async def _local_embed(self, text: str) -> list[float]:
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            embedding = model.encode(text)
            return embedding.tolist()
        except Exception as e:
            print(f"Local embed error: {e}")
            return []

    # ── Similarité cosinus (fallback JSONB) ───────────────────────────

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calcule la similarité cosinus — utilisé uniquement en mode JSONB."""
        import math
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


embedding_service = EmbeddingService()