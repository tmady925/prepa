"""
Scheduler de scraping — toutes les 6h.
Intégré dans le lifespan FastAPI comme simulation_scheduler.
"""
import asyncio
from datetime import datetime


async def run_scraping_scheduler():
    """Lance le scraping toutes les 6 heures."""
    print("🔍 Scraping scheduler démarré")
    while True:
        try:
            await asyncio.sleep(6 * 3600)  # 6h
            print(f"🔍 Scraping démarré: {datetime.now().isoformat()}")
            from app.db.database import AsyncSessionLocal
            from app.services.scraping_service import scraping_service
            async with AsyncSessionLocal() as db:
                result = await scraping_service.scrape_all(db)
                print(f"🔍 Scraping terminé: {result}")
        except asyncio.CancelledError:
            print("Scraping scheduler arrêté")
            break
        except Exception as e:
            print(f"Scraping scheduler error: {e}")
            await asyncio.sleep(300)  # retry après 5 min si erreur
