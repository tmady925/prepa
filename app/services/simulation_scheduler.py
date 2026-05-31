"""
Scheduler pour les simulations — vérifie toutes les minutes :
- J-1 : envoie les notifications
- Heure H : lance la simulation
- Après la durée : ferme et corrige
"""
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.simulation import Simulation
from app.services.simulation_service import simulation_service


async def check_simulations():
    """Vérifie et lance les actions nécessaires pour chaque simulation."""
    async with AsyncSessionLocal() as db:
        try:
            now = datetime.now(timezone.utc)

            # Récupère toutes les simulations actives ou programmées
            result = await db.execute(
                select(Simulation).where(
                    Simulation.statut.in_(["scheduled", "active"])
                )
            )
            simulations = result.scalars().all()

            for sim in simulations:
                date_debut = sim.date_debut
                if date_debut.tzinfo is None:
                    date_debut = date_debut.replace(tzinfo=timezone.utc)

                date_j1 = date_debut - timedelta(days=1)
                heure_fin = date_debut + timedelta(minutes=sim.duree_minutes)

                # Notif J-1
                if not sim.notif_j1_sent and now >= date_j1 and now < date_debut:
                    print(f"Scheduler: notif J-1 → {sim.titre}")
                    await simulation_service.envoyer_notif_j1(db, sim)

                # Lancement heure H
                if not sim.notif_debut_sent and now >= date_debut and sim.statut == "scheduled":
                    print(f"Scheduler: lancement → {sim.titre}")
                    await simulation_service.lancer_simulation(db, sim)

                # Fermeture et correction
                if sim.statut == "active" and now >= heure_fin and not sim.resultats_envoyes:
                    print(f"Scheduler: correction → {sim.titre}")
                    await simulation_service.corriger_toutes_copies(db, sim)

        except Exception as e:
            print(f"Scheduler error: {e}")


async def run_scheduler():
    """Boucle infinie — vérifie toutes les 60 secondes."""
    print("🕐 Simulation scheduler démarré")
    while True:
        await check_simulations()
        await asyncio.sleep(60)
