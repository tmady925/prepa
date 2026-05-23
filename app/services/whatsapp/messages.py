"""
Tous les messages envoyés aux élèves.
Centralisés ici pour faciliter les modifications et traductions futures.
"""
from datetime import datetime


class Messages:

    # ── Onboarding ────────────────────────────────────────────────

    WELCOME = (
        "👋 Bienvenue sur *Prepa* !\n\n"
        "Je suis ton assistant personnel de révision. "
        "Je vais t'aider à préparer ton examen étape par étape.\n\n"
        "Pour commencer — *comment tu t'appelles ?*"
    )

    def ask_exam(self, name: str) -> str:
        return (
            f"Enchanté *{name}* ! 🎓\n\n"
            "Tu prépares quel examen ?"
        )

    EXAM_BUTTONS = [
        {"id": "exam_bac", "title": "BAC"},
        {"id": "exam_bfem", "title": "BFEM"},
        {"id": "exam_concours", "title": "Concours"},
    ]

    def ask_series_bac(self, name: str) -> str:
        return f"Super {name} ! Tu es en quelle série ?"

    SERIES_BAC_BUTTONS = [
        {"id": "serie_s1", "title": "S1"},
        {"id": "serie_s2", "title": "S2"},
        {"id": "serie_l1", "title": "L1 / L2"},
    ]

    SERIES_BAC_LIST = {
        "button": "Choisir ma série",
        "sections": [
            {
                "title": "Sciences",
                "rows": [
                    {"id": "serie_s1", "title": "S1", "description": "Maths - Physique"},
                    {"id": "serie_s2", "title": "S2", "description": "SVT - Physique"},
                    {"id": "serie_s3", "title": "S3", "description": "Sciences de l'ingénieur"},
                ],
            },
            {
                "title": "Littéraire",
                "rows": [
                    {"id": "serie_l1", "title": "L1", "description": "Philosophie - Lettres"},
                    {"id": "serie_l2", "title": "L2", "description": "Langues"},
                ],
            },
            {
                "title": "Technique",
                "rows": [
                    {"id": "serie_t", "title": "T", "description": "Technique"},
                    {"id": "serie_steg", "title": "STEG", "description": "Sciences économiques"},
                ],
            },
        ],
    }

    def ask_subjects(self, name: str) -> str:
        return (
            f"Parfait {name} ! 📚\n\n"
            "Sur quelles matières tu veux te concentrer ?\n"
            "_(Tu pourras en ajouter d'autres plus tard)_\n\n"
            "Réponds avec les numéros séparés par des virgules :\n\n"
            "1 - Mathématiques\n"
            "2 - Physique-Chimie\n"
            "3 - SVT\n"
            "4 - Français\n"
            "5 - Philosophie\n"
            "6 - Histoire-Géo\n"
            "7 - Anglais"
        )

    def ask_exam_date(self) -> str:
        return (
            "📅 *Quand passe-tu ton examen ?*\n\n"
            "Réponds avec la date au format :\n"
            "*JJ/MM/AAAA*\n\n"
            "Exemple : 15/06/2026"
        )

    def onboarding_complete(self, name: str, days_left: int) -> str:
        return (
            f"✅ Tout est prêt *{name}* !\n\n"
            f"Il te reste *{days_left} jours* avant ton examen.\n\n"
            "Tu peux maintenant :\n"
            "• Poser des questions sur tes cours\n"
            "• Demander des exercices\n"
            "• Soumettre tes réponses pour correction\n\n"
            "Par quoi on commence ? 🚀"
        )

    # ── Quota ─────────────────────────────────────────────────────

    def quota_reached(self, name: str, referral_code: str) -> str:
        return (
            f"Tu as utilisé tous tes messages du jour *{name}* 🎯\n\n"
            "Deux options :\n\n"
            f"📤 *Invite des amis* et gagne 20 messages par ami actif :\n"
            f"_Partage ce code : *{referral_code}*_\n\n"
            "⭐ *Passe Pro* pour réviser sans limite :\n"
            "Seulement *500 FCFA/mois*"
        )

    QUOTA_BUTTONS = [
        {"id": "action_invite", "title": "Inviter des amis"},
        {"id": "action_pro", "title": "Passer Pro ⭐"},
    ]

    # ── Commandes spéciales ────────────────────────────────────────

    def help_message(self, name: str, days_left: int = 0) -> str:
        return (
            f"👋 Bonjour *{name}* ! Voici ce que tu peux faire :\n\n"
            "*Questions de cours*\n"
            "Pose n'importe quelle question sur tes matières\n\n"
            "*Exercices*\n"
            "« Donne-moi un exercice de maths »\n\n"
            "*Correction*\n"
            "« Corrige cet exercice : ... »\n\n"
            "*Commandes*\n"
            "/progression — voir ton avancement\n"
            "/inviter — gagner des messages gratuits\n"
            "/plan — changer ton abonnement\n"
            "/aide — afficher ce menu\n\n"
            f"_Il te reste *{days_left} jours* avant ton examen_ 📅"
        )

    def progression_message(self, user) -> str:
        days_left = 0
        if user.exam_date:
            exam_date = user.exam_date.replace(tzinfo=None)
            days_left = max(0, (exam_date - datetime.now()).days)

        streak_emoji = "🔥" if user.streak_days >= 3 else "📅"
        plan_emoji = "⭐" if user.plan == "pro" else "🆓"

        return (
            f"📊 *Ta progression, {user.name}*\n\n"
            f"{streak_emoji} Streak : *{user.streak_days} jours* consécutifs\n"
            f"💬 Messages total : *{user.total_messages}*\n"
            f"📚 Exercices faits : *{user.total_exercises_done}*\n"
            f"🎯 Score engagement : *{user.engagement_score}/100*\n"
            f"{plan_emoji} Plan : *{user.plan.upper()}*\n\n"
            f"⏳ Il te reste *{days_left} jours* avant ton examen\n\n"
            f"{'Continue comme ça ! 💪' if user.streak_days >= 3 else 'Reviens demain pour garder ta flamme ! 🔥'}"
        )

    def invite_message(self, user) -> str:
        return (
            f"🎁 *Invite tes amis et gagne des messages gratuits !*\n\n"
            f"Ton code de parrainage : *{user.referral_code}*\n\n"
            f"Comment ça marche :\n"
            f"1️⃣ Partage ce message à tes amis\n"
            f"2️⃣ Ils s'inscrivent avec ton code\n"
            f"3️⃣ Tu gagnes *20 messages* par ami actif\n"
            f"4️⃣ Tu gagnes *50 messages* si ton ami passe Pro\n\n"
            f"📤 Message à partager :\n"
            f"_« Salut ! J'utilise Prepa pour réviser mon {user.exam_type or 'examen'}. "
            f"C'est vraiment bien ! Inscris-toi avec mon code *{user.referral_code}* »_"
        )

    def plan_message(self, user) -> str:
        if user.plan == "pro":
            return (
                f"⭐ Tu es déjà *Prepa Pro* !\n\n"
                f"Tu révises sans limite jusqu'à la fin de ton abonnement.\n\n"
                f"Tape */progression* pour voir tes stats."
            )
        return (
            f"💡 *Passe Prepa Pro et révise sans limite !*\n\n"
            f"✅ Messages illimités\n"
            f"✅ LLM plus puissant\n"
            f"✅ Corrections détaillées\n"
            f"✅ Priorité de réponse\n\n"
            f"💰 Seulement *500 FCFA/mois*\n\n"
            f"Paiement via Wave, Orange Money ou Free Money 🔒"
        )

    # ── Erreurs ───────────────────────────────────────────────────

    NOT_UNDERSTOOD = (
        "Je n'ai pas bien compris 😅\n"
        "Peux-tu reformuler ta question ?"
    )

    ERROR = (
        "Une erreur s'est produite 😔\n"
        "Réessaie dans quelques instants."
    )


messages = Messages()