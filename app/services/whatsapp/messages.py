"""
Tous les messages envoyés aux élèves.
Centralisés ici pour faciliter les modifications et traductions futures.
"""
from datetime import datetime


class Messages:

    # ── Onboarding ────────────────────────────────────────────────

    WELCOME = (
        "👋 Bienvenue sur *Prepa* !\n\n"
        "Ton assistant personnel pour réussir tes études et ta carrière. "
        "Je vais t'accompagner dans tes révisions, tes concours et ta recherche d'emploi.\n\n"
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

    # ── Usage ──────────────────────────────────────────────────────

    def ask_usage(self, name: str) -> str:
        return (
            f"Parfait *{name}* ! 🎯\n\n"
            f"Tu utilises Prepa pour :"
        )

    USAGE_BUTTONS = [
        {"id": "usage_etudes", "title": "🎓 Préparer mon examen"},
        {"id": "usage_concours", "title": "🏆 Préparer un concours"},
        {"id": "usage_emploi", "title": "💼 Trouver un emploi"},
    ]

    USAGE_TOUT_BUTTON = {"id": "usage_tout", "title": "🎯 Tout à la fois"}

    # ── Concours ───────────────────────────────────────────────────

    def ask_type_concours(self, name: str) -> str:
        return f"Quel type de concours prépares-tu *{name}* ?"

    TYPE_CONCOURS_BUTTONS = [
        {"id": "concours_grandes_ecoles", "title": "🏫 Grandes écoles"},
        {"id": "concours_fonction_publique", "title": "🏛️ Fonction publique"},
        {"id": "concours_prive", "title": "🏢 Entreprises privées"},
    ]

    def ask_concours_cible(self, name: str) -> str:
        return (
            f"Quel concours tu cibles exactement *{name}* ?\n\n"
            f"Exemple : *ESP*, *Douanes*, *Police*, *BNDE*...\n"
            f"Écris le nom du concours."
        )

    def ask_date_concours(self) -> str:
        return (
            "📅 *Quand passe-tu ce concours ?*\n\n"
            "Format : *JJ/MM/AAAA*\n"
            "Exemple : 15/09/2026\n\n"
            "_Tape *passer* si tu ne connais pas encore la date._"
        )

    # ── Emploi ─────────────────────────────────────────────────────

    def ask_secteur_emploi(self, name: str) -> str:
        return (
            f"Dans quel domaine tu travailles ou veux travailler *{name}* ?\n\n"
            "Réponds avec les numéros séparés par des virgules :\n\n"
            "1 - Informatique / Tech\n"
            "2 - Finance / Comptabilité\n"
            "3 - Marketing / Communication\n"
            "4 - Santé\n"
            "5 - Éducation\n"
            "6 - BTP / Ingénierie\n"
            "7 - Droit / Juridique\n"
            "8 - Autre (précise)"
        )

    def ask_niveau_etudes(self, name: str) -> str:
        return f"Quel est ton niveau d'études *{name}* ?"

    NIVEAU_ETUDES_BUTTONS = [
        {"id": "niveau_bac", "title": "Bac ou moins"},
        {"id": "niveau_bac2", "title": "Bac+2 / BTS"},
        {"id": "niveau_bac3", "title": "Licence / Bac+3"},
    ]

    NIVEAU_ETUDES_BUTTONS_2 = [
        {"id": "niveau_bac5", "title": "Master / Bac+5"},
        {"id": "niveau_doctorat", "title": "Doctorat"},
    ]

    def ask_type_contrat(self, name: str) -> str:
        return f"Quel type de contrat recherches-tu *{name}* ?"

    TYPE_CONTRAT_BUTTONS = [
        {"id": "contrat_cdi", "title": "CDI"},
        {"id": "contrat_cdd", "title": "CDD"},
        {"id": "contrat_stage", "title": "Stage"},
    ]

    TYPE_CONTRAT_BUTTONS_2 = [
        {"id": "contrat_freelance", "title": "Freelance"},
        {"id": "contrat_indifferent", "title": "Peu importe"},
    ]

    def ask_localisation_emploi(self, name: str) -> str:
        return (
            f"Tu cherches un emploi où *{name}* ? 📍\n\n"
            f"Exemple : *Dakar*, *Thiès*, *Télétravail*, *Partout*"
        )

    def ask_cv_upload(self, name: str) -> str:
        return (
            f"Envoie ton *CV* en PDF ou photo *{name}* 📄\n\n"
            f"Je vais analyser ton profil pour te trouver "
            f"les meilleures opportunités.\n\n"
            f"_Tape *passer* si tu n'as pas de CV pour l'instant._"
        )

    # ── Profil complet ──────────────────────────────────────────────

    def profil_complet(self, user) -> str:
        usage = user.usage or []
        if isinstance(usage, str):
            usage = [usage]

        msg = f"📊 *Ton profil, {user.name}*\n\n"

        if "etudes" in usage or "tout" in usage:
            msg += "🎓 *ÉTUDES*\n"
            msg += f"Examen : {user.exam_type or 'Non défini'}"
            if user.series:
                msg += f" — {user.series}"
            msg += "\n"
            if user.subjects:
                msg += f"Matières : {', '.join(user.subjects)}\n"
            if user.exam_date:
                msg += f"Date : {user.exam_date.strftime('%d/%m/%Y')}\n"
            msg += "\n"

        if "concours" in usage or "tout" in usage:
            conv = user.conversation_state or {}
            msg += "🏆 *CONCOURS*\n"
            concours = conv.get("concours_cible") or "Non défini"
            msg += f"Concours : {concours}\n\n"

        if "emploi" in usage or "tout" in usage:
            msg += "💼 *EMPLOI*\n"
            if user.secteur_emploi:
                msg += f"Secteur : {', '.join(user.secteur_emploi)}\n"
            if user.niveau_etudes:
                msg += f"Niveau : {user.niveau_etudes}\n"
            if user.type_contrat_souhaite:
                msg += f"Contrat : {user.type_contrat_souhaite}\n"
            if user.localisation_emploi:
                msg += f"Localisation : {user.localisation_emploi}\n"
            msg += "\n"

        msg += "*Que veux-tu modifier ?*\n"
        return msg

    PROFIL_EDIT_BUTTONS = [
        {"id": "edit_etudes", "title": "✏️ Mes infos études"},
        {"id": "edit_emploi", "title": "💼 Mon profil emploi"},
    ]

    # ── Détection nouveau besoin ────────────────────────────────────

    def suggest_new_service(self, service: str) -> str:
        if service == "concours":
            return (
                "🏆 Tu parles de concours !\n\n"
                "Veux-tu que j'active la préparation concours pour toi ?"
            )
        elif service == "emploi":
            return (
                "💼 Tu cherches un emploi !\n\n"
                "Veux-tu que j'active la recherche d'emploi pour toi ?"
            )
        return ""

    SUGGEST_SERVICE_BUTTONS = [
        {"id": "confirm_new_service", "title": "✅ Oui, activer"},
        {"id": "ignore_service", "title": "❌ Non merci"},
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

    def ask_plan(self, name: str, usage=None) -> str:
        usage = usage or ["etudes"]
        if isinstance(usage, str):
            usage = [usage]
        s = set(usage)
        # Ligne "gratuit" adaptée au contexte
        if s == {"emploi"}:
            gratuit = "🆓 *Gratuit* — 1 offre d'emploi par semaine"
        elif s in ({"etudes"}, {"concours"}):
            gratuit = "🆓 *Gratuit* — 10 messages par jour"
        else:
            gratuit = "🆓 *Gratuit* — 10 messages/jour + 1 offre d'emploi/semaine"
        return (
            f"Presque fini *{name}* ! 🎉\n\n"
            "Comment veux-tu utiliser Prepa ?\n\n"
            f"{gratuit}\n"
            "⭐ *Pro* — *tout en illimité* (études · concours · emploi), 500 FCFA/mois\n\n"
            "_Tu peux toujours changer plus tard avec /plan_"
        )

    # ── Pro / Paiement ─────────────────────────────────────────────

    WAVE_PAY_LINK = "https://pay.wave.com/m/M_sn_CRUaBBWCzDPq/c/sn/?amount=500"

    def _pro_benefits_intro(self, context: str) -> str:
        if context == "emploi":
            return (
                "💼 *En gratuit* : 1 offre d'emploi par semaine.\n"
                "⭐ *En Pro* : offres d'emploi *illimitées*, dès qu'une correspond à ton profil."
            )
        if context == "concours":
            return (
                "🏆 *En gratuit* : 10 messages par jour pour ta prépa concours.\n"
                "⭐ *En Pro* : messages *illimités* + corrections détaillées."
            )
        if context == "etudes":
            return (
                "📚 *En gratuit* : 10 messages par jour, pas d'accès aux simulations d'examen.\n"
                "⭐ *En Pro* : messages *illimités* + corrections détaillées + simulations d'examen complètes."
            )
        return (
            "🆓 *En gratuit* : 10 messages/jour (études & concours) + 1 offre d'emploi/semaine.\n"
            "⭐ *En Pro* : tout en illimité."
        )

    def wave_fallback_block(self) -> str:
        return (
            f"👉 *Paie 500F avec Wave* en cliquant sur ce lien :\n{self.WAVE_PAY_LINK}\n\n"
            "⚠️ *Important* :\n"
            "• Paie avec le *numéro WhatsApp que tu utilises ici sur Prepa* 📱\n"
            "• Ajoute cet expéditeur à tes contacts pour rendre le lien cliquable\n"
            "• L'activation Pro se fait *sous 24h* ⏳\n"
            "• Tu recevras une *notification* dès que c'est activé ✅"
        )

    def pro_upsell(self, name: str, context: str = "tout", payment_url: str | None = None) -> str:
        """
        Message Pro adapté au contexte (emploi/etudes/concours/tout).
        Si payment_url fourni → lien PayDunya, sinon → fallback Wave.
        """
        msg = f"⭐ *Passe Prepa Pro, {name} !*\n\n"
        msg += self._pro_benefits_intro(context) + "\n\n"
        msg += (
            "✨ *500 FCFA/mois* débloquent l'accès *illimité à TOUT* :\n"
            "📚 Études · 🏆 Concours · 💼 Emploi\n\n"
        )
        if payment_url:
            msg += f"👉 Paie en ligne ici :\n{payment_url}\n\n"
            msg += "_Paiement sécurisé via Wave, Orange Money ou Free Money 🔒_"
        else:
            msg += self.wave_fallback_block()
        return msg

    PLAN_ONBOARDING_BUTTONS = [
        {"id": "onboarding_free", "title": "Gratuit 🆓"},
        {"id": "onboarding_pro", "title": "Passer Pro ⭐"},
    ]

    def onboarding_complete(self, name: str, days_left: int, usage=None) -> str:
        usage = usage or ["etudes"]
        if isinstance(usage, str):
            usage = [usage]

        msg = f"✅ Tout est prêt *{name}* !\n\n"

        if "etudes" in usage and days_left:
            msg += f"Il te reste *{days_left} jours* avant ton examen.\n\n"

        msg += "Tu peux maintenant :\n"
        if "etudes" in usage:
            msg += "• Demander des exercices 📝\n"
            msg += "• Soumettre tes réponses pour correction 📸\n"
        if "concours" in usage:
            msg += "• T'entraîner pour ton concours 🏆\n"
        if "emploi" in usage:
            msg += "• Recevoir des offres d'emploi adaptées 💼\n"

        msg += "\nPar quoi on commence ? 🚀"
        return msg

    # ── Quota ─────────────────────────────────────────────────────

    def quota_reached(self, name: str, context: str = "etudes") -> str:
        if context == "concours":
            intro = f"Tu as utilisé tes 10 messages du jour pour ta prépa concours *{name}* 🏆"
        else:
            intro = f"Tu as utilisé tous tes messages du jour *{name}* 🎯"
        return (
            f"{intro}\n\n"
            "Pour continuer aujourd'hui :\n\n"
            "📤 *Inviter des amis* → +20 messages & +1 offre emploi/semaine par ami actif\n"
            "⭐ *Passer Pro* → *tout en illimité* (études · concours · emploi) pour *500 FCFA/mois*"
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
            f"🎁 *Invite tes amis et gagne des récompenses !*\n\n"
            f"Partage ce lien à tes amis :\n"
            f"{wa_link}\n\n"
            f"Quand un ami rejoint avec ton lien et devient actif :\n"
            f"✅ *+20 messages* bonus\n"
            f"✅ *+1 offre d'emploi* supplémentaire par semaine\n\n"
            f"Si ton ami passe *Pro* :\n"
            f"✅ *+50 messages* bonus\n"
            f"✅ *+1 offre d'emploi* supplémentaire par semaine\n\n"
            f"Ton code : *{code}*"
        )

    def plan_message_pro(self, user) -> str:
        """Message quand l'utilisateur est déjà Pro."""
        return (
            f"⭐ Tu es déjà *Prepa Pro* !\n\n"
            f"Tu profites de *tout en illimité* (études · concours · emploi) "
            f"jusqu'à la fin de ton abonnement.\n\n"
            f"Tape */progression* pour voir tes stats."
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