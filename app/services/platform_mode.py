"""
Platform Mode — Mode de fonctionnement global de la plateforme
===============================================================

Gère le flag `emploi_only_mode` :
- Si False (défaut) → Prepa fonctionne normalement (études + concours + emploi)
- Si True → Prepa devient une plateforme EMPLOI UNIQUEMENT :
    * Plus d'examen ni de concours dans l'onboarding
    * Plus d'examen ni de concours dans les réponses du bot/LLM
    * L'utilisateur est traité comme un chercheur d'emploi partout

Ce module centralise la détection du mode pour éviter de dupliquer la logique.
Toute erreur de lecture config → retourne False (comportement normal préservé).
"""
from app.services.config_service import config_service


async def is_emploi_only() -> bool:
    """Retourne True si la plateforme est en mode emploi uniquement."""
    try:
        return await config_service.get_bool("emploi_only_mode")
    except Exception:
        return False


# ─── Fragments de prompt selon le mode ───────────────────────────────────────

DOMAINS_FULL = """Prepa couvre 3 domaines :
1. ÉTUDES : révisions BAC/BFEM, exercices, corrections, matières scolaires
2. CONCOURS : préparation aux concours (fonction publique, grandes écoles, privé)
3. EMPLOI : matching offres d'emploi, conseils CV, orientation professionnelle"""

DOMAINS_EMPLOI_ONLY = """Prepa est une plateforme dédiée à l'EMPLOI :
- Matching avec des offres d'emploi adaptées au profil
- Conseils CV, lettres de motivation, préparation aux entretiens
- Orientation professionnelle et développement de carrière

IMPORTANT : Tu n'abordes JAMAIS les études, les examens scolaires (BAC, BFEM)
ni les concours. Si l'utilisateur en parle, recentre poliment la conversation
sur sa recherche d'emploi et son projet professionnel."""


async def domains_description() -> str:
    """Retourne la description des domaines selon le mode actif."""
    if await is_emploi_only():
        return DOMAINS_EMPLOI_ONLY
    return DOMAINS_FULL
