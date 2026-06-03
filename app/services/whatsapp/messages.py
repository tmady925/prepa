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

    def ask_series_bac(self, name: str) -> str:
        return f"Super {name} ! Tu es en quelle série ?"

    # ── Onboarding dynamique ──────────────────────────────────────

    def ask_confirm_pays(self, name: str, pays_nom: str, flag: str) -> str:
        return (
            f"Bonjour *{name}* ! 👋\n\n"
            f"Je vois que tu es de *{pays_nom}* {flag}\n\n"
            f"C'est bien ça ?"
        )

    CONFIRM_PAYS_BUTTONS = [
        {"id": "pays_oui", "title": "✅ Oui"},
        {"id": "pays_non", "title": "❌ Non, autre pays"},
    ]

    def ask_pays_manuel(self) -> str:
        return (
            "Dans quel pays es-tu ? 🌍\n\n"
            "Réponds avec le nom de ton pays.\n"
            "Exemple : *Sénégal*, *Côte d'Ivoire*, *Mali*..."
        )

    def ask_exam_dynamic(self, name: str, exams: list) -> str:
        """exams = liste de dicts {code, name, pays}"""
        return f"Enchanté *{name}* ! 🎓\n\nTu prépares quel examen ?"

    def build_exam_buttons(self, exams: list) -> list:
        """Construit les boutons d'examens depuis la DB."""
        return [
            {"id": f"exam_{e['code']}", "title": e["name"][:20]}
            for e in exams[:3]  # WhatsApp limite à 3 boutons
        ]

    def build_exam_list(self, exams: list) -> dict:
        """Construit la liste d'examens si > 3."""
        rows = [
            {
                "id": f"exam_{e['code']}",
                "title": e["name"][:24],
                "description": e.get("pays", "")[:72],
            }
            for e in exams
        ]
        return {
            "button": "Choisir mon examen",
            "sections": [{"title": "Examens disponibles", "rows": rows}],
        }

    def build_series_list(self, series: list, exam_name: str) -> dict:
        """Construit la liste des séries d'un examen."""
        sciences   = [s for s in series if s["code"] in ("S1", "S2", "S3", "C", "D")]
        litteraire = [s for s in series if s["code"] in ("L1", "L2", "A")]
        technique  = [s for s in series if s["code"] in ("T", "STEG", "G")]
        autres     = [s for s in series if s not in sciences + litteraire + technique]

        def to_rows(lst):
            return [
                {
                    "id": f"serie_{s['code'].lower()}",
                    "title": s["code"],
                    "description": s.get("description", "")[:72],
                }
                for s in lst
            ]

        sections = []
        if sciences:
            sections.append({"title": "Sciences", "rows": to_rows(sciences)})
        if litteraire:
            sections.append({"title": "Littéraire", "rows": to_rows(litteraire)})
        if technique:
            sections.append({"title": "Technique", "rows": to_rows(technique)})
        if autres:
            sections.append({"title": "Autres", "rows": to_rows(autres)})

        return {
            "button": "Choisir ma série",
            "sections": sections if sections else [
                {"title": exam_name, "rows": to_rows(series)}
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

    def ask_plan(self, name: str) -> str:
        return (
            f"Presque fini *{name}* ! 🎉\n\n"
            "Comment veux-tu utiliser Prepa ?\n\n"
            "🆓 *Gratuit* — 10 messages par jour\n"
            "⭐ *Pro* — messages illimités, 500 FCFA/mois\n\n"
            "_Tu peux toujours changer plus tard avec /plan_"
        )

    PLAN_ONBOARDING_BUTTONS = [
        {"id": "onboarding_free", "title": "Gratuit 🆓"},
        {"id": "onboarding_pro", "title": "Passer Pro ⭐"},
    ]

    def onboarding_complete(self, name: str, days_left: int) -> str:
        return (
            f"✅ Tout est prêt *{name}* !\n\n"
            f"Il te reste *{days_left} jours* avant ton examen.\n\n"
            "Tu peux maintenant :\n"
            "• Demander des exercices\n"
            "• Soumettre tes réponses pour correction\n\n"
            "Par quoi on commence ? 🚀"
        )

    # ── Quota ─────────────────────────────────────────────────────

    def quota_reached(self, name: str) -> str:
        return (
            f"Tu as utilisé tous tes messages du jour *{name}* 🎯\n\n"
            "Pour continuer :\n\n"
            "📤 *Inviter des amis* → gagne 20 messages par ami actif\n"
            "⭐ *Passer Pro* → révise sans limite pour 3000 FCFA/mois"
        )

    QUOTA_BUTTONS = [
        {"id": "action_invite", "title": "Inviter des amis"},
        {"id": "action_pro", "title": "Passer Pro ⭐"},
    ]

    NEXT_EXERCISE_BUTTONS = [
        {"id": "next_exercise", "title": "Exercice suivant ➡️"},
        {"id": "action_profil", "title": "Voir ma progression 📊"},
    ]

    def feedback_suffix(
        self,
        score: int,
        retry_count: int,
        matiere: str,
        chapitre: str,
    ) -> str:
        """1-2 lignes d'orientation après le détail de correction."""
        chapitre_label = (chapitre or "").replace("_", " ").title() if chapitre else ""

        if score >= 70:
            if chapitre_label:
                return f"_Prêt pour un exercice plus difficile en *{chapitre_label}* ?_ 💪"
            return "_Prêt pour un exercice plus difficile ?_ 💪"
        elif score >= 40:
            return "_Un nouvel exercice du même niveau t'attend !_"
        else:
            if retry_count < 2:
                return "_Ne te décourage pas — on reprend cet exercice depuis le début._ 💪"
            return "_On va travailler les bases avec un exercice plus simple._"

    def all_exercises_done(self, name: str, matiere: str, chapitre: str) -> str:
        chapitre_label = (chapitre or "").replace("_", " ").title() if chapitre else ""
        matiere_label = (matiere or "").replace("_", " ").title() if matiere else ""
        return (
            f"🏆 *Félicitations {name} !*\n\n"
            f"Tu as fait tous les exercices disponibles"
            + (f" en *{chapitre_label}*" if chapitre_label else "")
            + (f" ({matiere_label})" if matiere_label else "")
            + ".\n\n"
            "Pose-moi une question de cours ou demande un exercice d'une autre matière ! 📚"
        )

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
        from app.core.settings import get_settings
        settings = get_settings()
        code = user.referral_code or ""
        wa_number = settings.whatsapp_number.replace("+", "").replace(" ", "")
        wa_link = f"https://wa.me/{wa_number}?text=PREPA-{code}"

        return (
            f"🎁 *Invite tes amis et gagne des messages gratuits !*\n\n"
            f"Partage ce lien à tes amis :\n"
            f"{wa_link}\n\n"
            f"Quand ils cliquent et s'inscrivent avec ton lien :\n"
            f"✅ Tu gagnes *20 messages* par ami actif\n"
            f"✅ Tu gagnes *50 messages* si ton ami passe Pro\n\n"
            f"Ton code : *{code}*"
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