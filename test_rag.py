import asyncio
from app.db.redis import connect_redis
from app.db.database import AsyncSessionLocal
from app.services.rag.search_service import search_service


async def test():
    await connect_redis()
    async with AsyncSessionLocal() as db:
        results = await search_service.search(
            db=db,
            query="reactions nucleaires",
            exam_type="bac_senegal",
            series="S2",
            top_k=3,
        )
        print(f"Resultats: {len(results)}")
        for r in results:
            print(f"Similarite: {r['similarity']:.3f}")
            print(f"Contenu: {r['content'][:150]}")
            print()


asyncio.run(test())