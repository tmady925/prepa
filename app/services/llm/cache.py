import json
import hashlib
from app.db.redis import get_redis
from app.core.settings import get_settings

settings = get_settings()


class SemanticCache:

    async def get(self, key: str) -> str | None:
        redis = await get_redis()
        cached = await redis.get(f"llm_cache:{key}")
        if cached:
            data = json.loads(cached)
            return data.get("response")
        return None

    async def set(self, key: str, response: str) -> None:
        redis = await get_redis()
        await redis.setex(
            f"llm_cache:{key}",
            settings.redis_cache_ttl,
            json.dumps({"response": response}),
        )

    def make_key(self, prompt: str, provider: str) -> str:
        normalized = prompt.lower().strip()
        raw = f"{provider}:{normalized}"
        return hashlib.sha256(raw.encode()).hexdigest()


semantic_cache = SemanticCache()