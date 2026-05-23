"""
Service d'embedding pour le RAG.
Provider principal : Mistral
Fallback : calcul local avec sentence-transformers
"""
import httpx
from app.core.settings import get_settings

settings = get_settings()


class EmbeddingService:

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

    async def _mistral_embed(self, text: str) -> list[float] | None:
        """Embedding via API Mistral."""
        if not settings.mistral_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-embed",
                        "input": [text],
                    },
                )
                data = response.json()
                return data["data"][0]["embedding"]
        except Exception as e:
            print(f"Mistral embed error: {e}")
            return None

    async def _mistral_embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embedding batch via API Mistral — plus efficace."""
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
                        json={
                            "model": "mistral-embed",
                            "input": batch,
                        },
                    )
                    data = response.json()
                    batch_embeddings = [item["embedding"] for item in data["data"]]
                    all_embeddings.extend(batch_embeddings)

            return all_embeddings
        except Exception as e:
            print(f"Mistral batch embed error: {e}")
            return None

    async def _local_embed(self, text: str) -> list[float]:
        """Fallback local avec sentence-transformers."""
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            embedding = model.encode(text)
            return embedding.tolist()
        except Exception as e:
            print(f"Local embed error: {e}")
            return []

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calcule la similarité cosinus entre deux vecteurs."""
        import math
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


embedding_service = EmbeddingService()