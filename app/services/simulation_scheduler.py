"""Simulation scheduler — désactivé (module emploi uniquement)."""
import asyncio


async def run_scheduler():
    """No-op scheduler — simulations supprimées."""
    while True:
        await asyncio.sleep(3600)
