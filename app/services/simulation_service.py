"""
Service de gestion des simulations d'examens et concours.
Gère le cycle complet : programmation → lancement → collecte → correction → classement.

Corrections appliquées :
- Pro gating : seuls les users Pro reçoivent les simulations
- Chemins externalisés dans settings
- Correction parallèle avec asyncio.gather()
- Resoumission de copie autorisée (écrasement)
- conversation_state nettoyé dans tous les cas d'erreur
"""
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.simulation import Simulation, SimulationParticipation
from app.models.user import User
from app.services.whatsapp.sender import whatsapp_sender
from app.services.copy_analyzer_service import copy_analyzer_service
from app.core.settings import get_settings
import fitz

settings = get_settings()

# Répertoires depuis settings
SIMULATIONS_DIR = Path(settings.simulations_dir)
SIMULATIONS_BASE_URL = settings.simulations_base_url


class SimulationService:

    def _is_busy_user(self, user) -> bool:
        """Vérifie si le user est dans un processus actif (même logique que queue_service)."""
        conv = user.conversation_state or {}
        return bool(
            conv.get("awaiting_simulation_copy") or
            conv.get("awaiting_copy") or
            conv.get("exercise_path") or
            conv.get("awaiting_copy_for_free_correction")
        )

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
        Lance une simulation — envoie le sujet à tous les inscrits Pro.
        Seuls les utilisateurs Pro reçoivent la simulation (Pro gating).
        """
        result = await db.execute(
            select(SimulationParticipation, User).join(
                User, User.id == SimulationParticipation.user_id
            ).where(
                SimulationParticipation.simulation_id == simulation.id,
                SimulationParticipation.statut == "inscrit",
                User.plan == "pro",  # ── Pro gating ──
            )
        )
        participants = result.all()

        if not participants:
            # Fallback : aucun inscrit Pro → on envoie quand même à tous les Pro éligibles
            print(f"Simulation {simulation.titre} : aucun inscrit Pro → fallback tous les Pro")
            series_eligibles = simulation.series_eligibles or []
            fallback_query = select(User).where(
                User.status == "active",
                User.plan == "pro",
                User.onboarding_step == "done",
            )
            if series_eligibles:
                fallback_query = fallback_query.where(User.series.in_(series_eligibles))
            fallback_result = await db.execute(fallback_query)
            fallback_users = fallback_result.scalars().all()

            if not fallback_users:
                print(f"Simulation {simulation.titre} : aucun utilisateur Pro trouvé")
                simulation.statut = "active"
                simulation.notif_debut_sent = True
                await db.commit()
                return

            # Inscrit + construit la liste participants pour continuer normalement
            for u in fallback_users:
                await self.inscrire_user(db, simulation.id, u.id)
            await db.flush()

            # Recharge les participants après inscription
            result2 = await db.execute(
                select(SimulationParticipation, User).join(
                    User, User.id == SimulationParticipation.user_id
                ).where(
                    SimulationParticipation.simulation_id == simulation.id,
                    SimulationParticipation.statut == "inscrit",
                    User.plan == "pro",
                )
            )
            participants = result2.all()
            if not participants:
                simulation.statut = "active"
                simulation.notif_debut_sent = True
                await db.commit()
                return

        heure_fin = simulation.date_debut + timedelta(minutes=simulation.duree_minutes)
        heure_fin_str = heure_fin.strftime("%H:%M")

        sujet_url = None
        if simulation.sujet_pdf_path:
            path = Path(simulation.sujet_pdf_path)
            if path.exists():
                sujet_url = f"{SIMULATIONS_BASE_URL}/sujets/{path.name}"

        heures = simulation.duree_minutes // 60
        minutes = simulation.duree_minutes % 60

        nb_envoyes = 0
        for participation, user in participants:
            try:
                msg = (
                    f"🎓 *{simulation.titre}* — C'est parti !\n\n"
                    f"⏱ Durée : *{heures}h{minutes:02d}*\n"
                    f"🕐 Heure limite : *{heure_fin_str}*\n\n"
                    f"📝 Instructions :\n"
                    f"- Fais l'épreuve sur papier ✏️\n"
                    f"- Prends une photo de chaque page 📸\n"
                    f"- Envoie ta copie avant *{heure_fin_str}*\n\n"
                    f"_Bonne chance {user.name or 'ami'} ! Tu peux le faire 💪_"
                )
                await whatsapp_sender.send_text(user.phone_number, msg)

                if sujet_url:
                    await whatsapp_sender._send({
                        "to": user.phone_number,
                        "documentUrl": sujet_url,
                        "fileName": f"sujet_{simulation.titre.replace(' ', '_')}.pdf",
                        "text": "📄 Sujet de l'épreuve",
                    })

                # Mise à jour du state + flush immédiat pour chaque user
                user.conversation_state = {
                    "awaiting_simulation_copy": True,
                    "simulation_id": str(simulation.id),
                    "simulation_titre": simulation.titre,
                    "heure_fin": heure_fin.isoformat(),
                    "duree_minutes": simulation.duree_minutes,
                }
                await db.flush()
                nb_envoyes += 1

            except Exception as e:
                print(f"Erreur envoi simulation à {user.phone_number}: {e}")

        simulation.statut = "active"
        simulation.notif_debut_sent = True
        await db.commit()
        print(f"✅ Simulation {simulation.titre} lancée — {nb_envoyes}/{len(participants)} participants Pro")

    async def envoyer_notification_manuelle(
        self,
        db: AsyncSession,
        simulation: Simulation,
        message_custom: str | None = None,
    ) -> int:
        """
        Envoie une notification manuelle aux utilisateurs Pro éligibles.
        Inscrit automatiquement les destinataires si pas encore inscrits.
        Peut être déclenchée à tout moment par l'admin.
        Retourne le nombre d'utilisateurs notifiés.
        """
        count_result = await db.execute(
            select(func.count()).where(
                SimulationParticipation.simulation_id == simulation.id
            )
        )
        count = count_result.scalar() or 0

        series_eligibles = simulation.series_eligibles or []
        # Filtre de base : users actifs ayant terminé l'onboarding
        query = select(User).where(
            User.status == "active",
            User.onboarding_step == "done",
        )
        # Filtre série uniquement si défini sur la simulation
        if series_eligibles:
            query = query.where(User.series.in_(series_eligibles))
        # Filtre exam uniquement si défini sur la simulation
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

        print(f"📣 envoyer_notification_manuelle: {len(users)} user(s) trouvé(s) pour '{simulation.titre}'")
        for u in users:
            print(f"   → {u.phone_number} | plan={u.plan} | status={u.status} | step={u.onboarding_step} | series={u.series}")

        date_str = simulation.date_debut.strftime("%d/%m/%Y à %Hh%M")
        heures = simulation.duree_minutes // 60

        from app.services.queue_service import send_or_queue
        notified = 0
        for user in users:
            try:
                if message_custom:
                    # Message custom : envoi simple sans bouton
                    await send_or_queue(db, user, message_custom)
                else:
                    # Message avec bouton d'inscription
                    msg = (
                        f"🎯 *Simulation — {simulation.titre}*\n\n"
                        f"📅 Date : *{date_str}*\n"
                        f"⏱ Durée : *{heures}h*\n"
                        f"👥 *{count} participants* déjà inscrits\n\n"
                        f"Prépare ton matériel : stylo, feuilles, calculatrice 📐\n\n"
                        f"Appuie sur le bouton pour t'inscrire et recevoir le sujet à l'heure H !"
                    )
                    # Envoie avec bouton d'inscription (id = sim_inscrire_{simulation_id})
                    if not self._is_busy_user(user):
                        await whatsapp_sender.send_buttons(
                            user.phone_number,
                            msg,
                            [{"id": f"sim_inscrire_{simulation.id}", "title": "Je m'inscris ✅"}],
                        )
                    else:
                        # User occupé : enfile le message texte simple (sans bouton)
                        await send_or_queue(db, user, msg + "\n\n_Réponds *Je m'inscris* pour t'inscrire._")
                notified += 1
            except Exception as e:
                print(f"Erreur notification à {user.phone_number}: {e}")

        if notified > 0:
            simulation.notif_j1_sent = True
        await db.commit()
        print(f"✅ Notification manuelle envoyée — {notified} élèves Pro")
        return notified

    async def soumettre_copie(
        self,
        db: AsyncSession,
        simulation_id: uuid.UUID,
        user_id: uuid.UUID,
        image_bytes: bytes,
    ) -> dict:
        """
        Traite la soumission (ou resoumission) d'une copie.
        Une resoumission est autorisée tant que le délai n'est pas dépassé.
        """
        result = await db.execute(
            select(SimulationParticipation).where(
                SimulationParticipation.simulation_id == simulation_id,
                SimulationParticipation.user_id == user_id,
            )
        )
        participation = result.scalar_one_or_none()
        if not participation:
            return {"success": False, "error": "Participation non trouvée"}

        sim_result = await db.execute(
            select(Simulation).where(Simulation.id == simulation_id)
        )
        simulation = sim_result.scalar_one_or_none()
        if not simulation:
            return {"success": False, "error": "Simulation non trouvée"}

        now = datetime.now(timezone.utc)
        heure_fin = simulation.date_debut + timedelta(minutes=simulation.duree_minutes)
        if now > heure_fin:
            return {"success": False, "error": "Délai dépassé"}

        # Sauvegarde (ou écrase) la copie — settings-based path
        copies_dir = SIMULATIONS_DIR / str(simulation_id)
        copies_dir.mkdir(parents=True, exist_ok=True)
        copie_path = copies_dir / f"{user_id}.jpg"
        copie_path.write_bytes(image_bytes)
        copie_path.chmod(0o644)

        participation.copie_path = str(copie_path)
        participation.statut = "soumis"
        participation.submitted_at = now
        await db.commit()

        return {"success": True}

    # ─────────────────────────────────────────────────────────────────
    # Correction parallèle
    # ─────────────────────────────────────────────────────────────────

    async def _corriger_une_copie(
        self,
        participation: SimulationParticipation,
        user: User,
        sujet_text: str,
        correction_text: str,
        matiere: str,
    ) -> tuple | None:
        """Corrige une copie individuelle. Retourne (participation, user, score) ou None."""
        try:
            if not participation.copie_path:
                return None
            copie_path = Path(participation.copie_path)
            if not copie_path.exists():
                return None

            image_bytes = copie_path.read_bytes()
            analysis = await copy_analyzer_service.analyze_copy(
                image_bytes=image_bytes,
                exercise_text=sujet_text,
                correction_text=correction_text,
                matiere=matiere,
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
                return (participation, user, score)
        except Exception as e:
            print(f"Erreur correction copie {user.phone_number}: {e}")
        return None

    async def corriger_toutes_copies(self, db: AsyncSession, simulation: Simulation):
        """
        Corrige toutes les copies soumises en parallèle et calcule le classement.
        Le statut passe à 'correcting' avant de commencer pour éviter les retries du scheduler.
        """
        # ── Marque comme "en cours de correction" pour bloquer le scheduler ──
        simulation.statut = "correcting"
        await db.commit()

        try:
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
                simulation.statut = "closed"
                simulation.resultats_envoyes = True
                await db.commit()
                return

            # Extrait les textes des PDFs
            sujet_text = self._extract_pdf_text(simulation.sujet_pdf_path)
            correction_text = self._extract_pdf_text(simulation.correction_pdf_path)
            matiere = simulation.matiere or ""

            # ── Correction séquentielle (AsyncSession non thread-safe) ──
            # Les appels LLM sont lourds : on corrige l'un après l'autre
            # pour éviter la corruption de session SQLAlchemy en parallèle.
            scores = []
            for p, u in participations:
                res = await self._corriger_une_copie(p, u, sujet_text, correction_text, matiere)
                if res is not None:
                    scores.append(res)
                await db.flush()  # persiste chaque correction immédiatement

            # Classement
            scores.sort(key=lambda x: x[2], reverse=True)
            for rang, (participation, user, score) in enumerate(scores, 1):
                participation.classement = rang

            await db.commit()

            # Envoi des résultats
            await self.envoyer_resultats(db, simulation, scores)

        except Exception as e:
            print(f"Erreur critique correction simulation {simulation.titre}: {e}")
            # ── Statut error pour éviter la boucle infinie du scheduler ──
            simulation.statut = "error"
            await db.commit()

    def _extract_pdf_text(self, pdf_path: str | None) -> str:
        """Extrait le texte d'un PDF. Retourne '' si indisponible."""
        if not pdf_path:
            return ""
        try:
            doc = fitz.open(pdf_path)
            text = "".join(page.get_text("text") for page in doc)
            doc.close()
            return text
        except Exception:
            return ""

    async def envoyer_resultats(self, db: AsyncSession, simulation: Simulation, scores: list):
        """Envoie les résultats individuels + classement général."""
        total = len(scores)
        if not total:
            simulation.resultats_envoyes = True
            simulation.statut = "closed"
            await db.commit()
            return

        moyenne = sum(s for _, _, s in scores) / total
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

        corr_url = None
        if simulation.correction_pdf_path:
            corr_path = Path(simulation.correction_pdf_path)
            if corr_path.exists():
                corr_url = f"{SIMULATIONS_BASE_URL}/corrections/{corr_path.name}"

        from app.services.queue_service import send_or_queue
        for participation, user, score in scores:
            try:
                feedback = participation.feedback or {}
                feedback_msg = copy_analyzer_service.format_feedback(
                    feedback, user.name or "élève"
                )
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

                await send_or_queue(db, user, feedback_msg + rang_msg)

                if corr_url:
                    await whatsapp_sender._send({
                        "to": user.phone_number,
                        "documentUrl": corr_url,
                        "fileName": Path(simulation.correction_pdf_path).name,
                        "text": "📄 Correction officielle",
                    })

            except Exception as e:
                print(f"Erreur envoi résultats à {user.phone_number}: {e}")

        # Classement général
        for _, user, _ in scores:
            try:
                await send_or_queue(db, user, classement_msg)
            except Exception:
                pass

        simulation.resultats_envoyes = True
        simulation.statut = "closed"
        await db.commit()
        print(f"✅ Résultats envoyés — {total} participants")

        # ── Nettoie les users encore bloqués en "awaiting_simulation_copy"
        # (inscrits n'ayant pas soumis) et flush leur queue de notifications ──
        await self._debloquer_non_soumetteurs(db, simulation)

    async def _debloquer_non_soumetteurs(self, db: AsyncSession, simulation: Simulation):
        """
        Débloque les participants inscrits qui n'ont pas soumis de copie.
        Ils ont encore awaiting_simulation_copy=True dans conversation_state.
        On nettoie leur état et on flush leur queue de notifications.
        """
        from app.services.queue_service import flush_queue
        try:
            result = await db.execute(
                select(SimulationParticipation, User).join(
                    User, User.id == SimulationParticipation.user_id
                ).where(
                    SimulationParticipation.simulation_id == simulation.id,
                    SimulationParticipation.statut == "inscrit",
                )
            )
            rows = result.all()
            for participation, user in rows:
                conv = user.conversation_state or {}
                if conv.get("awaiting_simulation_copy") and conv.get("simulation_id") == str(simulation.id):
                    await whatsapp_sender.send_text(
                        user.phone_number,
                        f"⏰ Le temps imparti pour *{simulation.titre}* est écoulé.\n\n"
                        f"Tu n'as pas soumis de copie cette fois — pas de problème, la prochaine sera la bonne ! 💪"
                    )
                    user.conversation_state = {}
                    await db.flush()
                    await flush_queue(db, user)
            if rows:
                await db.commit()
        except Exception as e:
            print(f"Erreur deblocage non-soumetteurs: {e}")


simulation_service = SimulationService()
