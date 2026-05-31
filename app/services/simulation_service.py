"""
Service de gestion des simulations d'examens et concours.
Gère le cycle complet : programmation → lancement → collecte → correction → classement.
"""
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.simulation import Simulation, SimulationParticipation
from app.models.user import User
from app.services.whatsapp.sender import whatsapp_sender
from app.services.copy_analyzer_service import copy_analyzer_service
from app.core.settings import get_settings
import fitz

settings = get_settings()


class SimulationService:

    async def get_active_simulations(self, db: AsyncSession) -> list[Simulation]:
        """Retourne les simulations scheduled ou active."""
        result = await db.execute(
            select(Simulation).where(
                Simulation.statut.in_(["scheduled", "active"])
            ).order_by(Simulation.date_debut)
        )
        return result.scalars().all()

    async def inscrire_user(self, db: AsyncSession, simulation_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Inscrit un élève à une simulation."""
        existing = await db.execute(
            select(SimulationParticipation).where(
                SimulationParticipation.simulation_id == simulation_id,
                SimulationParticipation.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            return False  # Déjà inscrit

        participation = SimulationParticipation(
            simulation_id=simulation_id,
            user_id=user_id,
            statut="inscrit",
        )
        db.add(participation)
        await db.flush()
        return True

    async def lancer_simulation(self, db: AsyncSession, simulation: Simulation):
        """
        Lance une simulation — envoie le sujet à tous les inscrits.
        """
        # Récupère les participants
        result = await db.execute(
            select(SimulationParticipation, User).join(
                User, User.id == SimulationParticipation.user_id
            ).where(
                SimulationParticipation.simulation_id == simulation.id,
                SimulationParticipation.statut == "inscrit",
            )
        )
        participants = result.all()

        if not participants:
            print(f"Simulation {simulation.titre} : aucun participant")
            return

        heure_fin = simulation.date_debut + timedelta(minutes=simulation.duree_minutes)
        heure_fin_str = heure_fin.strftime("%H:%M")

        sujet_url = None
        if simulation.sujet_pdf_path:
            path = Path(simulation.sujet_pdf_path)
            if path.exists():
                sujet_url = f"http://72.62.4.97/simulations/{path.name}"

        for participation, user in participants:
            try:
                # Message de lancement
                msg = (
                    f"🎓 *{simulation.titre}* — C'est parti !\n\n"
                    f"⏱ Durée : *{simulation.duree_minutes // 60}h{simulation.duree_minutes % 60:02d}*\n"
                    f"🕐 Heure limite : *{heure_fin_str}*\n\n"
                    f"📝 Instructions :\n"
                    f"- Fais l'épreuve sur papier ✏️\n"
                    f"- Prends une photo de chaque page 📸\n"
                    f"- Envoie ta copie avant *{heure_fin_str}*\n\n"
                    f"_Bonne chance {user.name or 'ami'} ! Tu peux le faire 💪_"
                )
                await whatsapp_sender.send_text(user.phone_number, msg)

                # Envoie le sujet PDF
                if sujet_url:
                    await whatsapp_sender._send({
                        "to": user.phone_number,
                        "documentUrl": sujet_url,
                        "fileName": f"sujet_{simulation.titre.replace(' ', '_')}.pdf",
                        "text": "📄 Sujet de l'épreuve",
                    })

                # Met à jour le conversation_state pour attendre la copie
                user.conversation_state = {
                    "awaiting_simulation_copy": True,
                    "simulation_id": str(simulation.id),
                    "simulation_titre": simulation.titre,
                    "heure_fin": heure_fin.isoformat(),
                    "duree_minutes": simulation.duree_minutes,
                }

            except Exception as e:
                print(f"Erreur envoi simulation à {user.phone_number}: {e}")

        # Met à jour le statut
        simulation.statut = "active"
        simulation.notif_debut_sent = True
        await db.commit()
        print(f"✅ Simulation {simulation.titre} lancée — {len(participants)} participants")

    async def envoyer_notif_j1(self, db: AsyncSession, simulation: Simulation):
        """Envoie la notification J-1."""
        # Compte les inscrits
        count_result = await db.execute(
            select(func.count()).where(
                SimulationParticipation.simulation_id == simulation.id
            )
        )
        count = count_result.scalar() or 0

        # Récupère les élèves éligibles (par série)
        series_eligibles = simulation.series_eligibles or []
        query = select(User).where(User.status == "active")
        if series_eligibles:
            query = query.where(User.series.in_(series_eligibles))
        if simulation.exam_id:
            from app.models.exam import Exam
            exam_result = await db.execute(
                select(Exam).where(Exam.id == simulation.exam_id)
            )
            exam = exam_result.scalar_one_or_none()
            if exam:
                query = query.where(User.exam_type == exam.code)

        users_result = await db.execute(query)
        users = users_result.scalars().all()

        date_str = simulation.date_debut.strftime("%d/%m/%Y à %Hh%M")

        for user in users:
            try:
                msg = (
                    f"🔔 *Simulation {simulation.titre}*\n\n"
                    f"📅 Demain : *{date_str}*\n"
                    f"⏱ Durée : *{simulation.duree_minutes // 60}h*\n"
                    f"👥 *{count} participants* déjà inscrits\n\n"
                    f"Réponds *OUI* pour confirmer ta participation !\n\n"
                    f"_Prépare ton matériel : stylo, feuilles, calculatrice 📐_"
                )
                await whatsapp_sender.send_text(user.phone_number, msg)

                # Inscrit automatiquement si pas encore inscrit
                await self.inscrire_user(db, simulation.id, user.id)

            except Exception as e:
                print(f"Erreur notif J-1 à {user.phone_number}: {e}")

        simulation.notif_j1_sent = True
        await db.commit()
        print(f"✅ Notif J-1 envoyée — {len(users)} élèves")

    async def soumettre_copie(
        self,
        db: AsyncSession,
        simulation_id: uuid.UUID,
        user_id: uuid.UUID,
        image_bytes: bytes,
    ) -> dict:
        """Traite la soumission d'une copie."""
        # Récupère la participation
        result = await db.execute(
            select(SimulationParticipation).where(
                SimulationParticipation.simulation_id == simulation_id,
                SimulationParticipation.user_id == user_id,
            )
        )
        participation = result.scalar_one_or_none()
        if not participation:
            return {"success": False, "error": "Participation non trouvée"}

        if participation.statut == "soumis":
            return {"success": False, "error": "Copie déjà soumise"}

        # Récupère la simulation
        sim_result = await db.execute(
            select(Simulation).where(Simulation.id == simulation_id)
        )
        simulation = sim_result.scalar_one_or_none()
        if not simulation:
            return {"success": False, "error": "Simulation non trouvée"}

        # Vérifie le délai
        now = datetime.now(timezone.utc)
        heure_fin = simulation.date_debut + timedelta(minutes=simulation.duree_minutes)
        if now > heure_fin:
            return {"success": False, "error": "Délai dépassé"}

        # Sauvegarde la copie
        copies_dir = Path(f"/home/prepa/app/simulations/{simulation_id}")
        copies_dir.mkdir(parents=True, exist_ok=True)
        copie_path = copies_dir / f"{user_id}.jpg"
        copie_path.write_bytes(image_bytes)
        copie_path.chmod(0o644)

        participation.copie_path = str(copie_path)
        participation.statut = "soumis"
        participation.submitted_at = now
        await db.commit()

        return {"success": True}

    async def corriger_toutes_copies(self, db: AsyncSession, simulation: Simulation):
        """Corrige toutes les copies soumises et calcule le classement."""
        result = await db.execute(
            select(SimulationParticipation, User).join(
                User, User.id == SimulationParticipation.user_id
            ).where(
                SimulationParticipation.simulation_id == simulation.id,
                SimulationParticipation.statut == "soumis",
            )
        )
        participations = result.all()

        if not participations:
            print(f"Simulation {simulation.titre} : aucune copie à corriger")
            return

        # Extrait le texte du sujet et de la correction
        sujet_text = ""
        correction_text = ""

        if simulation.sujet_pdf_path:
            try:
                doc = fitz.open(simulation.sujet_pdf_path)
                for page in doc:
                    sujet_text += page.get_text("text")
                doc.close()
            except Exception:
                pass

        if simulation.correction_pdf_path:
            try:
                doc = fitz.open(simulation.correction_pdf_path)
                for page in doc:
                    correction_text += page.get_text("text")
                doc.close()
            except Exception:
                pass

        scores = []

        for participation, user in participations:
            try:
                if not participation.copie_path:
                    continue

                copie_path = Path(participation.copie_path)
                if not copie_path.exists():
                    continue

                image_bytes = copie_path.read_bytes()

                # Analyse avec Mistral Vision
                analysis = await copy_analyzer_service.analyze_copy(
                    image_bytes=image_bytes,
                    exercise_text=sujet_text,
                    correction_text=correction_text,
                    matiere=simulation.matiere or "",
                    chapitre="",
                    niveau=2,
                    student_name=user.name or "élève",
                )

                if analysis:
                    score = analysis.get("score", 0)
                    participation.score = score
                    participation.mention = analysis.get("mention", "")
                    participation.feedback = analysis
                    participation.statut = "corrigé"
                    participation.corrected_at = datetime.now(timezone.utc)
                    scores.append((participation, user, score))

            except Exception as e:
                print(f"Erreur correction copie {user.phone_number}: {e}")

        await db.flush()

        # Calcule le classement
        scores.sort(key=lambda x: x[2], reverse=True)
        for rang, (participation, user, score) in enumerate(scores, 1):
            participation.classement = rang

        await db.commit()

        # Envoie les résultats individuels
        await self.envoyer_resultats(db, simulation, scores)

    async def envoyer_resultats(self, db: AsyncSession, simulation: Simulation, scores: list):
        """Envoie les résultats individuels + classement général."""
        total = len(scores)
        moyenne = sum(s for _, _, s in scores) / total if total else 0

        # Top 3
        top3 = scores[:3]
        medailles = ["🥇", "🥈", "🥉"]

        classement_msg = (
            f"🏆 *Résultats — {simulation.titre}*\n\n"
            f"👥 {total} participants\n"
            f"📊 Moyenne : {moyenne:.1f}/100\n\n"
            f"*Podium :*\n"
        )
        for i, (_, user, score) in enumerate(top3):
            classement_msg += f"{medailles[i]} {user.name or 'Élève'} — {score}/100\n"

        # Envoie le classement à tous
        for participation, user, score in scores:
            try:
                # Feedback individuel
                feedback = participation.feedback or {}
                feedback_msg = copy_analyzer_service.format_feedback(
                    feedback, user.name or "élève"
                )

                # Classement personnel
                rang_msg = (
                    f"\n\n🎯 *Ton classement : {participation.classement}/{total}*\n"
                    f"Score : *{score}/100*\n"
                )
                if participation.classement == 1:
                    rang_msg += "\n🏆 Félicitations, tu es premier(e) ! 🎉"
                elif participation.classement <= 3:
                    rang_msg += "\n⭐ Excellent résultat, tu es dans le top 3 !"
                elif participation.classement <= total // 2:
                    rang_msg += "\n💪 Tu es dans la première moitié, continue !"
                else:
                    rang_msg += "\n📚 Tu peux progresser, ne lâche pas !"

                await whatsapp_sender.send_text(
                    user.phone_number,
                    feedback_msg + rang_msg
                )

                # Envoie la correction PDF si disponible
                if simulation.correction_pdf_path:
                    corr_path = Path(simulation.correction_pdf_path)
                    if corr_path.exists():
                        corr_url = f"http://72.62.4.97/simulations/corrections/{corr_path.name}"
                        await whatsapp_sender._send({
                            "to": user.phone_number,
                            "documentUrl": corr_url,
                            "fileName": corr_path.name,
                            "text": "📄 Correction officielle",
                        })

            except Exception as e:
                print(f"Erreur envoi résultats à {user.phone_number}: {e}")

        # Envoie le classement général à tous
        for _, user, _ in scores:
            try:
                await whatsapp_sender.send_text(user.phone_number, classement_msg)
            except Exception:
                pass

        simulation.resultats_envoyes = True
        simulation.statut = "closed"
        await db.commit()
        print(f"✅ Résultats envoyés — {total} participants")


simulation_service = SimulationService()
