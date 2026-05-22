"""
Tous les messages envoyés aux élèves.
Centralisés ici pour faciliter les modifications et traductions futures.
"""


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