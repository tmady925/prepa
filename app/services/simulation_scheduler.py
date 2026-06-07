"""
Scheduler pour les simulations — vérifie toutes les minutes :
- J-1 : envoie les notifications
- Heure H : lance la simulation
- Après la durée : ferme et corrige

Corrections appliquées :
- Statut "correcting" évite les retries infinis du scheduler
- Statut "error" arrête les tentatives sur simulation en échec
- Protection contre les appels concurrents via _en_cours
"""
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.simulation import Simulation
from app.services.simulation_service import simulation_service

# ── Set des IDs en cours de traitement (anti-concurrence en mémoire) ──
_en_cours: set[str] = set()


async def check_simulations():
    """Vérifie et lance les actions nécessaires pour chaque simulation."""
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
