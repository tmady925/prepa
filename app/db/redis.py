import redis.asyncio as aioredis
from app.core.settings import get_settings

settings = get_settings()

redis_client: aioredis.Redis = None


async def get_redis() -> aioredis.Redis:
    return redis_client


async def connect_redis():
    global redis_client
    redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    await redis_client.ping()
    print("Connexion Redis OK")


async def disconnect_redis():
    global redis_client
    if redis_client:
        await redis_client.aclose()