"""
Service d'embedding pour le RAG.
Provider principal : Mistral embed (1024 dim)
Fallback : BAAI/bge-m3 local (1024 dim) — compatible pgvector
Stockage : pgvector (natif) ou JSONB (fallback)
"""
import asyncio
import httpx
from app.core.settings import get_settings

settings = get_settings()

# Détecte pgvector
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
        """
        Vérifie si pgvector est disponible.
        Utilise une connexion indépendante pour ne pas polluer
        la transaction courante en cas d'erreur.
        """
        if self._pgvector_checked:
            return self._pgvector_enabled

        try:
            from sqlalchemy import text
            # Connexion indépendante — ne touche pas la transaction active
            async with db.bind.connect() as conn:
                await conn.execute(text("SELECT '[1,2,3]'::vector"))
            self._pgvector_enabled = True
            print("pgvector: disponible ✅")
        except Exception:
            self._pgvector_enabled = False
            print("pgvector: non disponible — fallback JSONB")

        self._pgvector_checked = True
        return self._pgvector_enabled

    # ── Embed public ──────────────────────────────────────────────────

    async def embed_text(self, text: str) -> list[float] | None:
        """Génère l'embedding d'un texte — Mistral puis fallback local."""
        embedding = await self._mistral_embed(text)
        if embedding:
            return embedding
        print("Mistral embed failed — fallback local bge-m3")
        return await self._local_embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Génère les embeddings en batch — Mistral avec retry puis fallback."""
        embeddings = await self._mistral_embed_batch(texts)
        if embeddings and len(embeddings) == len(texts):
            return embeddings

        print(f"Mistral batch failed — fallback local pour {len(texts)} textes")
        results = []
        for text in texts:
            emb = await self._local_embed(text)
            results.append(emb if emb else [0.0] * EMBEDDING_DIM)
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
                if "data" not in data:
                    print(f"Mistral embed réponse inattendue: {data}")
                    return None
                emb = data["data"][0]["embedding"]
                assert len(emb) == EMBEDDING_DIM, f"Dims incorrectes: {len(emb)}"
                return emb
        except Exception as e:
            print(f"Mistral embed error: {e}")
            return None

    async def _mistral_embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Batch avec délai anti-rate-limit et retry automatique."""
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
                            print(f"  → Batch {batch_num}/{total_batches} ✅ ({len(batch)} textes)")
                            success = True
                            break

                        except Exception as e:
                            print(f"  Batch {batch_num} attempt {attempt+1} error: {e}")
                            await asyncio.sleep(2)

                    if not success:
                        print(f"  Batch {batch_num} échoué après {max_retries} tentatives")
                        return None

                    if i + batch_size < len(texts):
                        await asyncio.sleep(1.0)

            return all_embeddings

        except Exception as e:
            print(f"Mistral batch embed error: {e}")
            return None

    # ── Fallback local — BAAI/bge-m3 (1024 dims) ─────────────────────

    async def _local_embed(self, text: str) -> list[float]:
        """Fallback local BAAI/bge-m3 — 1024 dims compatible pgvector."""
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
            if len(result) != EMBEDDING_DIM:
                print(f"⚠️ Fallback dims incorrectes: {len(result)} au lieu de {EMBEDDING_DIM}")
                if len(result) < EMBEDDING_DIM:
                    result = result + [0.0] * (EMBEDDING_DIM - len(result))
                else:
                    result = result[:EMBEDDING_DIM]
            return result
        except Exception as e:
            print(f"Local embed error: {e}")
            return [0.0] * EMBEDDING_DIM

    def _get_local_model(self):
        """Charge le modèle local une seule fois en mémoire."""
        if self._local_model is not None:
            return self._local_model
        try:
            from sentence_transformers import SentenceTransformer
            print("Chargement modèle local BAAI/bge-m3 (1024 dims)...")
            self._local_model = SentenceTransformer("BAAI/bge-m3")
            print("Modèle local chargé ✅")
            return self._local_model
        except Exception as e:
            print(f"Erreur chargement modèle local: {e}")
            return None

    # ── Similarité cosinus (mode JSONB) ──────────────────────────────

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calcule la similarité cosinus — mode JSONB uniquement."""
        import math
        if len(vec1) != len(vec2):
            print(f"⚠️ Dimensions incompatibles: {len(vec1)} vs {len(vec2)}")
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)


embedding_service = EmbeddingService()