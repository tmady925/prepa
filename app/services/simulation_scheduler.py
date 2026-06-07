"""
Scheduler pour les simulations — vérifie toutes les minutes :
- Heure H  : lance la simulation (sujet envoyé aux inscrits Pro)
- Après durée : ferme et corrige les copies soumises

Protections :
- Statut "correcting" évite les retries infinis du scheduler
- Statut "error" arrête les tentatives sur simulation en échec
- Protection contre les appels concurrents via _en_cours (single-process)
- Recovery au démarrage : simulations bloquées en "correcting" → "error"
"""
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.simulation import Simulation
from app.services.simulation_service import simulation_service

# ── Set des IDs en cours de traitement (anti-concurrence intra-process) ──
_en_cours: set[str] = set()
_startup_done = False


async def _recover_stuck_simulations():
    """
    Au démarrage, passe toute simulation bloquée en 'correcting' vers 'error'.
    Évite qu'un crash de process laisse la simulation indéfiniment bloquée.
    """
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(Simulation).where(Simulation.statut == "correcting")
            )
            stuck = result.scalars().all()
            for sim in stuck:
                print(f"⚠️ Scheduler: simulation '{sim.titre}' était bloquée en 'correcting' → passage en 'error'")
                sim.statut = "error"
            if stuck:
                await db.commit()
        except Exception as e:
            print(f"Scheduler recovery error: {e}")


async def check_simulations():
    """Vérifie et lance les actions nécessaires pour chaque simulation."""
    global _startup_done
    if not _startup_done:
        await _recover_stuck_simulations()
        _startup_done = True

    async with AsyncSessionLocal() as db:
        try:
            now = datetime.now(timezone.utc)

            result = await db.execute(
                select(Simulation).where(
                    Simulation.statut.in_(["scheduled", "active"])
                )
            )
            simulations = result.scalars().all()

            for sim in simulations:
                sim_key = str(sim.id)

                # Ignore les simulations déjà en traitement dans cette instance
                if sim_key in _en_cours:
                    continue

                date_debut = sim.date_debut
                if date_debut.tzinfo is None:
                    date_debut = date_debut.replace(tzinfo=timezone.utc)

                heure_fin = date_debut + timedelta(minutes=sim.duree_minutes)

                # Lancement automatique à l'heure H
                if not sim.notif_debut_sent and now >= date_debut and sim.statut == "scheduled":
                    print(f"Scheduler: lancement → {sim.titre}")
                    _en_cours.add(sim_key)
                    try:
                        await simulation_service.lancer_simulation(db, sim)
                    finally:
                        _en_cours.discard(sim_key)

                # Correction automatique après la durée
                elif sim.statut == "active" and now >= heure_fin and not sim.resultats_envoyes:
                    print(f"Scheduler: correction → {sim.titre}")
                    _en_cours.add(sim_key)
                    try:
                        await simulation_service.corriger_toutes_copies(db, sim)
                    finally:
                        _en_cours.discard(sim_key)

        except Exception as e:
            print(f"Scheduler error: {e}")


async def run_scheduler():
    """Boucle infinie — vérifie toutes les 60 secondes."""
    print("🕐 Simulation scheduler démarré")
    while True:
        await check_simulations()
        await asyncio.sleep(60)
