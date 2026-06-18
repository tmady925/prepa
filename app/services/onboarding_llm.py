"""
Onboarding LLM Intelligence Layer
==================================
Ce module ajoute une couche d'intelligence LLM à chaque étape de l'onboarding.

PRINCIPE :
- Le state machine de l'onboarding reste INTACT (webhook.py inchangé dans sa logique)
- Avant chaque traitement, on passe par analyze() qui retourne un dict :
    {
        "action": "proceed" | "guide" | "answer" | "clarify",
        "value":  <valeur nettoyée à utiliser à la place du texte brut>,
        "message": <message à envoyer au user, ou None si pas besoin>,
        "stay": True | False,  # si True, ne pas avancer à l'étape suivante
    }

ACTIONS :
- "proceed" : valeur extraite et propre, continuer le flow normalement avec value
- "guide"   : cas particulier (pas de CV, date passée...), envoyer message et rester sur l'étape
- "answer"  : question hors flow, répondre et rester sur l'étape
- "clarify" : ambiguïté, demander précision et rester sur l'étape

SÉCURITÉ :
- Toute exception retourne {"action": "proceed", "value": text, "message": None}
  → le comportement actuel est préservé en cas d'erreur LLM
"""
import json
import re
from datetime import datetime, date
from typing import Any

import httpx
from app.core.settings import get_settings

settings = get_settings()

# ─── Specs par étape ────────────────────────────────────────────────────────

STEP_SPECS = {
    "name": {
        "objective": "Récupérer le prénom de l'utilisateur",
        "expected": "Un prénom (1 ou 2 mots max)",
        "rules": [
            "Extraire UNIQUEMENT le prénom, sans phrase",
            "Première lettre en majuscule, reste en minuscule",
            "Si le message est 'moi c'est Khady' → extraire 'Khady'",
            "Si le message est 'je m'appelle Moussa Diop' → extraire 'Moussa'",
            "Si le message ne contient pas de prénom identifiable (ex: chiffres, emojis seuls) → action=clarify",
        ],
        "platform_rules": [],
    },

    "confirm_pays": {
        "objective": "Confirmer ou corriger le pays détecté automatiquement",
        "expected": "Oui ou Non",
        "rules": [
            "Détecter toute formulation affirmative : 'oui', 'yes', 'ok', 'c'est ça', 'correct', 'exactement'... → action=proceed, value='oui'",
            "Détecter toute formulation négative : 'non', 'no', 'pas moi', 'pas ça', 'erreur'... → action=proceed, value='non'",
            "Si ambiguïté → action=clarify",
        ],
        "platform_rules": [],
    },

    "saisie_pays": {
        "objective": "Récupérer le pays de l'utilisateur",
        "expected": "Un nom de pays",
        "rules": [
            "Extraire le nom du pays depuis la réponse libre",
            "Accepter les variantes : 'je suis au Sénégal', 'Dakar' → Sénégal, 'Côte d'Ivoire', 'CI'...",
            "Si pays non reconnu → accept quand même, ne pas bloquer",
        ],
        "platform_rules": [],
    },

    "usage": {
        "objective": "Savoir pourquoi l'utilisateur utilise Prepa",
        "expected": "Un ou plusieurs usages parmi : études, concours, emploi, tout",
        "rules": [
            "Détecter 'études' : 'bac', 'réviser', 'examen', 'scolaire', 'cours'...",
            "Détecter 'concours' : 'concours', 'fonction publique', 'grande école', 'ENA', 'DGID'...",
            "Détecter 'emploi' : 'travail', 'job', 'boulot', 'cherche un poste', 'cv'...",
            "Détecter 'tout' : 'les deux', 'tout', 'les trois', 'tout ça'...",
            "Si l'utilisateur pose une question sur la plateforme → action=answer",
            "Si vraiment pas clair → action=clarify",
        ],
        "platform_rules": [
            "Choisir 'études' donne accès aux exercices et corrections",
            "Choisir 'concours' permet de préparer les concours spécifiques",
            "Choisir 'emploi' active le matching avec les offres d'emploi",
        ],
    },

    "type_concours": {
        "objective": "Quel type de concours prépare l'utilisateur",
        "expected": "grandes_ecoles | fonction_publique | prive",
        "rules": [
            "Détecter 'grandes_ecoles' : 'grande école', 'UCAD', 'ISM', 'ESP', 'IAM', 'école supérieure'...",
            "Détecter 'fonction_publique' : 'fonction publique', 'ENA', 'DGID', 'ENOA', 'armée', 'police', 'gendarmerie'...",
            "Détecter 'prive' : 'privé', 'entreprise', 'banque', 'assurance', 'BFI', 'BICIS'...",
            "Si ambiguïté ou question → action=clarify ou answer",
        ],
        "platform_rules": [],
    },

    "concours_cible": {
        "objective": "Nom précis du concours visé",
        "expected": "Le nom du concours ou de l'institution",
        "rules": [
            "Accepter n'importe quelle réponse textuelle non vide",
            "Nettoyer les phrases : 'je veux passer le concours de la DGID' → 'DGID'",
            "Si l'utilisateur ne sait pas encore → action=guide avec conseil",
        ],
        "platform_rules": [],
    },

    "date_concours": {
        "objective": "Date prévue du concours",
        "expected": "Une date au format JJ/MM/AAAA ou 'passer'",
        "rules": [
            "Si date valide et dans le futur → action=proceed, value=date au format JJ/MM/AAAA",
            "Si date dans le passé → action=guide, expliquer que la date est passée",
            "Accepter 'je sais pas', 'pas encore fixée', 'plus tard' → action=proceed, value='passer'",
            "Accepter des formulations naturelles : 'en juin 2026' → 01/06/2026, 'l'année prochaine' → estimer",
            "Si format invalide mais date probable → corriger et confirmer",
        ],
        "platform_rules": [],
    },

    "emploi_secteur": {
        "objective": "Secteur(s) d'activité recherché(s)",
        "expected": "Numéros ou noms de secteurs",
        "rules": [
            "Secteurs entreprise : Informatique/Tech, Finance/Comptabilité, Marketing/Communication, Santé, Éducation, BTP/Ingénierie, Droit/Juridique",
            "Secteur petit job : si le user mentionne livraison, manutention, vente ambulante, nettoyage, gardiennage, jardinage, baby-sitting, aide ménagère, coursier, porteur, vigile → retourner 'Petits jobs/missions courtes'",
            "Langage naturel entreprise : 'banque'→Finance/Comptabilité, 'développeur'→Informatique/Tech, 'avocat'→Droit/Juridique, 'médecin'→Santé, 'prof'→Éducation, 'construction'→BTP/Ingénierie",
            "Si le user dit '8' ou 'petits jobs' ou 'missions courtes' → retourner 'Petits jobs/missions courtes'",
            "Accepter 'peu importe', 'tout' → retourner 'Autre'",
            "Si l'utilisateur exprime une confusion sur ce qu'il veut faire → action=guide pour l'aider à choisir",
            "Si le user décrit un métier non classable → retourner le nom du métier tel quel",
        ],
        "platform_rules": [
            "Le secteur 'Petits jobs/missions courtes' oriente vers les missions de courte durée (livraison, manutention...)",
            "Les autres secteurs orientent vers les offres d'entreprise classiques",
        ],
    },

    "emploi_niveau": {
        "objective": "Niveau d'études de l'utilisateur",
        "expected": "bac | bac+2 | bac+3 | bac+5 | doctorat",
        "rules": [
            "Mapper : 'BTS'→bac+2, 'DUT'→bac+2, 'licence'→bac+3, 'master'→bac+5, 'ingénieur'→bac+5, 'phd'→doctorat",
            "Accepter 'pas encore de diplôme', 'étudiant' → action=guide avec conseil adapté, puis bac comme valeur",
            "Accepter 'en cours' → action=guide et stocker le niveau en cours",
        ],
        "platform_rules": [
            "Le niveau d'études est utilisé pour filtrer les offres adaptées",
        ],
    },

    "emploi_contrat": {
        "objective": "Type de contrat souhaité",
        "expected": "CDI | CDD | Stage | Freelance | indifferent",
        "rules": [
            "Détecter les variantes : 'temps plein'→CDI, 'mission'→Freelance, 'prestation'→Freelance",
            "Accepter 'peu importe', 'n'importe quoi'→indifferent",
            "Si étudiant ayant dit bac+2 ou moins → suggérer Stage s'il dit 'je sais pas'",
        ],
        "platform_rules": [],
    },

    "emploi_localisation": {
        "objective": "Lieu de travail souhaité",
        "expected": "Ville ou région",
        "rules": [
            "Accepter n'importe quelle ville/région",
            "Normaliser : 'je suis à Dakar' → 'Dakar', 'n'importe où' → 'Flexible'",
            "Accepter 'télétravail', 'remote'→ stocker 'Remote'",
        ],
        "platform_rules": [],
    },

    "emploi_cv": {
        "objective": "Récupérer le CV ou accepter de continuer sans",
        "expected": "Un fichier PDF/image ou une confirmation de passer",
        "rules": [
            "Si l'utilisateur dit qu'il n'a pas de CV : 'j'ai pas de cv', 'non', 'pas de cv', 'aucun'... → action=guide avec conseils + proposer de passer",
            "Si l'utilisateur demande comment faire un CV → action=answer avec conseils pratiques",
            "Si l'utilisateur dit 'passer', 'skip', 'plus tard', 'continuer sans'... → action=proceed, value='passer'",
            "Si l'utilisateur envoie du texte aléatoire qui n'est pas une commande connue → action=guide pour rappeler d'envoyer un fichier ou taper 'passer'",
        ],
        "platform_rules": [
            "Sans CV, le matching emploi fonctionne quand même mais est moins précis",
            "L'utilisateur peut envoyer son CV plus tard via /profil",
        ],
    },

    "exam": {
        "objective": "Quel examen prépare l'élève",
        "expected": "Un code d'examen parmi ceux disponibles",
        "rules": [
            "Si l'élève pose une question sur les examens ou la plateforme → action=answer",
            "Si l'élève exprime une hésitation ('je sais pas', 'les deux')→ action=clarify avec explication des options",
        ],
        "platform_rules": [],
    },

    "subjects": {
        "objective": "Matières que l'élève veut réviser",
        "expected": "Numéros séparés par virgules : 1=maths, 2=physique_chimie, 3=svt, 4=français, 5=philosophie, 6=histoire_geo, 7=anglais",
        "rules": [
            "Mapper langage naturel : 'maths'→1, 'physique'→2, 'svt'→3, 'français'→4, 'philo'→5, 'histoire'→6, 'geo'→6, 'anglais'→7",
            "Accepter 'toutes' ou 'tout' → '1,2,3,4,5,6,7'",
            "Accepter 'pc' → 2, 'hg'→6, 'svt'→3, 'maths-physique'→'1,2'",
            "Si l'élève ne sait pas quoi choisir → action=guide pour l'aider selon sa série",
            "Retourner value sous forme de string numérique ex: '1,3,4'",
        ],
        "platform_rules": [
            "Les matières choisies déterminent quels exercices l'élève reçoit",
        ],
    },

    "exam_date": {
        "objective": "Date de l'examen de l'élève",
        "expected": "Une date au format JJ/MM/AAAA",
        "rules": [
            "Si date valide et dans le futur → action=proceed, value=date au format JJ/MM/AAAA",
            "Si date dans le passé → action=guide, expliquer que la date est passée et demander correction",
            "Accepter 'je sais pas', 'plus tard', 'bientôt' → action=proceed, value='passer'",
            "Accepter 'juin 2026' → convertir en '01/06/2026'",
            "Si format invalide mais date probable → corriger et retourner la date corrigée",
        ],
        "platform_rules": [
            "Sans date d'examen, les rappels et le compte à rebours ne fonctionnent pas",
        ],
    },
}

# ─── Prompt système ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT_HEAD_FULL = """Tu es l'assistant intelligent de Prepa, une plateforme WhatsApp d'accompagnement pour les jeunes en Afrique.

Prepa couvre 3 domaines :
1. ÉTUDES : révisions BAC/BFEM, exercices, corrections, matières scolaires
2. CONCOURS : préparation aux concours (fonction publique, grandes écoles, privé)
3. EMPLOI : matching offres d'emploi, conseils CV, orientation professionnelle"""

_SYSTEM_PROMPT_HEAD_EMPLOI = """Tu es l'assistant emploi de Prepa, une plateforme WhatsApp dédiée à la recherche d'emploi pour les jeunes en Afrique.

Prepa accompagne UNIQUEMENT sur l'emploi :
- Matching avec des offres d'emploi adaptées au profil
- Conseils CV, lettre de motivation, entretiens
- Orientation professionnelle

IMPORTANT : Tu ne parles JAMAIS d'études, d'examens scolaires (BAC, BFEM) ni de concours.
Si l'utilisateur en parle, recentre poliment sur sa recherche d'emploi."""

_SYSTEM_PROMPT_BODY = """

Tu guides l'utilisateur pendant son inscription (onboarding).
Tu dois comprendre ses réponses de manière intelligente, même si elles sont informelles, en wolof mélangé, abrégées ou hors du format attendu.

RÈGLES IMPORTANTES :
- Tu réponds TOUJOURS en JSON valide, rien d'autre
- Tu es bienveillant, encourageant, adapté à un jeune africain
- Tu utilises des emojis avec parcimonie
- Si tu n'es pas sûr → préfère "proceed" avec la meilleure valeur extraite plutôt que de bloquer
- En cas de doute total → retourne {"action": "proceed", "value": null, "message": null, "stay": false}

FORMAT DE RÉPONSE (JSON strict) :
{
  "action": "proceed" | "guide" | "answer" | "clarify",
  "value": <string ou null>,
  "message": <string ou null>,
  "stay": <true ou false>
}

- "proceed" + value non null → valeur extraite propre, continuer le flow
- "proceed" + value null → pas de valeur claire, laisser le webhook gérer
- "guide" → cas particulier, envoyer message et rester sur l'étape
- "answer" → répondre à une question et rester sur l'étape
- "clarify" → demander clarification et rester sur l'étape
"""

# Compose les deux variantes du prompt système
SYSTEM_PROMPT = _SYSTEM_PROMPT_HEAD_FULL + _SYSTEM_PROMPT_BODY
SYSTEM_PROMPT_EMPLOI = _SYSTEM_PROMPT_HEAD_EMPLOI + _SYSTEM_PROMPT_BODY


# ─── Appel LLM léger ─────────────────────────────────────────────────────────

async def _call_llm_json(prompt: str, system: str = None) -> dict | None:
    """Appel LLM minimal pour obtenir un JSON. Essaie Groq puis Mistral."""
    messages = [
        {"role": "system", "content": system or SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    providers = []
    if getattr(settings, "groq_api_key", None):
        providers.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                          "llama-3.3-70b-versatile", settings.groq_api_key))
    if getattr(settings, "mistral_api_key", None):
        providers.append(("mistral", "https://api.mistral.ai/v1/chat/completions",
                          "mistral-small-latest", settings.mistral_api_key))

    for name, url, model, key in providers:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": 300, "temperature": 0.2},
                )
                if resp.status_code != 200:
                    continue
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                # Nettoyer les blocs ```json ... ```
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
                return json.loads(raw)
        except Exception as e:
            print(f"  [onboarding_llm] provider {name} error: {e}")
            continue

    return None


# ─── Fonction principale ─────────────────────────────────────────────────────

async def analyze(
    step: str,
    text: str,
    user_name: str = "",
    user_context: dict = None,
    today: date = None,
) -> dict:
    """
    Analyse le message de l'utilisateur pour une étape donnée de l'onboarding.

    Retourne toujours un dict avec les clés :
        action  : "proceed" | "guide" | "answer" | "clarify"
        value   : valeur propre à utiliser (ou None)
        message : message à envoyer au user (ou None)
        stay    : bool — True si on doit rester sur l'étape courante

    En cas d'erreur, retourne le fallback sécurisé (proceed sans value).
    """
    if not text or not text.strip():
        return _fallback()

    spec = STEP_SPECS.get(step)
    if not spec:
        # Étape non couverte → laisser le webhook gérer normalement
        return _fallback()

    today_str = (today or date.today()).strftime("%d/%m/%Y")
    ctx = user_context or {}

    # Construction du prompt
    rules_str = "\n".join(f"  - {r}" for r in spec["rules"])
    platform_str = ""
    if spec.get("platform_rules"):
        platform_str = "\nRÈGLES PLATEFORME :\n" + "\n".join(f"  - {r}" for r in spec["platform_rules"])

    ctx_str = ""
    if ctx:
        ctx_str = "\nCONTEXTE UTILISATEUR :\n" + "\n".join(f"  - {k}: {v}" for k, v in ctx.items() if v)

    prompt = f"""ÉTAPE ONBOARDING : {step}
OBJECTIF : {spec['objective']}
FORMAT ATTENDU : {spec['expected']}
DATE DU JOUR : {today_str}

RÈGLES DE TRAITEMENT :
{rules_str}{platform_str}
{ctx_str}
PRÉNOM UTILISATEUR : {user_name or 'inconnu'}
MESSAGE REÇU : "{text}"

Analyse ce message selon les règles ci-dessus et retourne le JSON approprié.
Sois intelligent et bienveillant. Si le message est clair, extrais la valeur et retourne action=proceed.
"""

    try:
        # Sélectionne le prompt système selon le mode plateforme
        _system = SYSTEM_PROMPT
        try:
            from app.services.platform_mode import is_emploi_only
            if await is_emploi_only():
                _system = SYSTEM_PROMPT_EMPLOI
        except Exception:
            pass

        result = await _call_llm_json(prompt, system=_system)
        if not result or "action" not in result:
            return _fallback()

        # Validation du résultat
        action = result.get("action", "proceed")
        if action not in ("proceed", "guide", "answer", "clarify"):
            return _fallback()

        return {
            "action": action,
            "value": result.get("value"),
            "message": result.get("message"),
            "stay": bool(result.get("stay", action != "proceed")),
        }

    except Exception as e:
        print(f"  [onboarding_llm] analyze error for step={step}: {e}")
        return _fallback()


def _fallback() -> dict:
    """Retourne le comportement par défaut sécurisé : laisser le webhook gérer."""
    return {"action": "proceed", "value": None, "message": None, "stay": False}


# ─── Déduction emploi_type ────────────────────────────────────────────────────

_EMPLOI_TYPE_SIGNALS = {
    "petit_job": [
        "petits jobs", "missions courtes", "manutention", "nettoyage",
        "gardiennage", "livraison", "vente ambulante", "agent de sécurité",
        "bricolage", "déménagement", "plomberie", "électricité", "peinture",
        "restauration rapide", "coursier", "vigile", "porteur",
    ],
    "entreprise": [
        "informatique", "finance", "comptabilité", "marketing", "communication",
        "droit", "juridique", "ingénierie", "btp", "santé", "éducation",
    ],
}

_NIVEAU_TO_TYPE = {
    "bac": "les_deux",
    "bac+2": "les_deux",
    "bac+3": "entreprise",
    "bac+5": "entreprise",
    "doctorat": "entreprise",
}


async def infer_emploi_type(user) -> str:
    """
    Déduit le type d'emploi préféré depuis le profil collecté.
    Retourne : "petit_job" | "entreprise" | "les_deux"

    Appel LLM avec fallback heuristique si LLM échoue.
    """
    niveau = (getattr(user, "niveau_etudes", None) or "").lower()
    secteurs = getattr(user, "secteur_emploi", None) or []
    if isinstance(secteurs, str):
        secteurs = [secteurs]
    contrat = (getattr(user, "type_contrat_souhaite", None) or "").lower()
    localisation = getattr(user, "localisation_emploi", None) or ""

    secteurs_str = ", ".join(secteurs).lower() if secteurs else "non défini"

    prompt = f"""Profil d'un chercheur d'emploi en Afrique de l'Ouest :
- Niveau d'études : {niveau or 'non défini'}
- Secteurs d'intérêt : {secteurs_str}
- Type de contrat souhaité : {contrat or 'non défini'}
- Localisation : {localisation or 'non définie'}

Ce chercheur correspond-il mieux à :
- "petit_job"   : missions courtes, travail ponctuel, revenus rapides (livraison, gardiennage, manutention…)
- "entreprise"  : poste salarié structuré (CDI/CDD/Stage dans une entreprise)
- "les_deux"    : profil mixte ou pas d'indication claire

Réponds UNIQUEMENT avec ce JSON :
{{"emploi_type": "petit_job" | "entreprise" | "les_deux"}}"""

    try:
        result = await _call_llm_json(prompt)
        if result and result.get("emploi_type") in ("petit_job", "entreprise", "les_deux"):
            return result["emploi_type"]
    except Exception as e:
        print(f"  [infer_emploi_type] LLM error: {e}")

    # Heuristique de secours
    secteurs_concat = secteurs_str
    has_petit_signal = any(s in secteurs_concat for s in _EMPLOI_TYPE_SIGNALS["petit_job"])
    has_entreprise_signal = any(s in secteurs_concat for s in _EMPLOI_TYPE_SIGNALS["entreprise"])

    if has_petit_signal and not has_entreprise_signal:
        return "petit_job"
    if has_entreprise_signal and not has_petit_signal:
        if contrat in ("cdi", "cdd", "stage"):
            return "entreprise"
    if niveau in _NIVEAU_TO_TYPE:
        return _NIVEAU_TO_TYPE[niveau]

    return "les_deux"


# ─── Conversation libre onboarding emploi ───────────────────────────────────

_CONVERSE_SYSTEM = """Tu es l'assistant emploi de Prepa, plateforme WhatsApp pour l'emploi et les petits boulots en Afrique de l'Ouest.

MISSION : définir le profil emploi de l'user en MAX 4 échanges. Direct, efficace, humain.
Adapte-toi à sa langue (français, wolof mélangé, abréviations — tout est ok).

━━━ RÈGLES ABSOLUES ━━━
- JAMAIS de salutation (Bonjour, Salut, Bonsoir, Hello). Ni au début, ni au milieu.
- Max 2 phrases par message.
- Plusieurs infos dans un message → mémorise TOUT, ne redemande jamais ce qui est déjà connu.
- Si tu manques de plusieurs infos et qu'il reste ≤ 2 tours → groupe les questions.
- Ne mentionne JAMAIS de noms de champs techniques.
- Ne promets JAMAIS un emploi, un salaire, un délai, un résultat. Tu connectes, tu ne garantis rien.
- Reste TOUJOURS sur le sujet emploi. Ne réponds à rien d'autre.

━━━ DÉTECTION DU RÔLE (dès le 1er message) ━━━
→ "demandeur" : cherche du travail
→ "offreur"   : veut recruter / propose un job
→ "les_deux"  : les deux
→ null        : pas clair → pose une question directe pour trancher

Si "offreur" → confirme en UNE phrase courte → done=true.

━━━ GESTION DE L'INATTENDU ━━━
- Hors-sujet ("tu fais quoi ce soir ?") → recadre gentiment en 1 phrase, reviens à la question. N'extrais rien.
- Flou ("je sais pas") → propose 2 pistes simples sans forcer.
- Message vide/non informatif ("ok", "hm", "bonjour") → relance la dernière question. N'extrais rien.
- Multi-infos en une phrase → extrais tous les champs d'un coup, saute les questions déjà répondues.
- Contradiction → garde la DERNIÈRE valeur donnée.

━━━ CHAMPS À EXTRAIRE (schéma STRICT) ━━━
Mets null si l'info n'est pas donnée. N'invente jamais.
{
  "role": "demandeur" | "offreur" | "les_deux" | null,
  "secteur_emploi": string | null,
  "localisation": string | null,
  "niveau_etudes": "aucun"|"bac"|"bac+2"|"bac+3"|"bac+5"|"doctorat" | null,
  "type_contrat": "cdi"|"cdd"|"stage"|"freelance"|"mission_courte"|"indifferent" | null,
  "emploi_type": "petit_job" | "entreprise" | "les_deux" | null
}

RÈGLE emploi_type (déduis-le, ne le demande pas frontalement) :
- mission courte / journalier / "vite" / "dépanner" → "petit_job"
- poste / CDI / carrière / long terme → "entreprise"
- les deux ou ambigu → "les_deux"

MINIMUM pour terminer (demandeur) : role + secteur_emploi + localisation + emploi_type.

━━━ FIN DE CONVERSATION ━━━
done=true quand : minimum atteint, OU offreur confirmé, OU tours restants = 0.
Le message de clôture est une confirmation courte, sans promesse.

━━━ FORMAT (JSON STRICT, rien d'autre) ━━━
{ "message": "...", "extracted": { ...schéma ci-dessus... }, "done": false }"""

_CONVERSE_OPENER = """Nouveau user sur la section emploi de Prepa.
Prénom : {name} | Pays : {pays} | Tours dispo : {max_turns}

Génère le PREMIER message. RÈGLES ABSOLUES :
- INTERDIT : toute salutation (Bonjour, Salut, Bonsoir, Hello...).
- Commence par le prénom ou directement par la question.
- UNE seule question, ouverte, courte, qui sert à détecter le rôle.
- Ne promets rien.

Exemple de ton : "{name}, tu cherches du travail ou tu veux recruter quelqu'un ?"

FORMAT (JSON STRICT) :
{{ "message": "...", "extracted": {{}}, "done": false }}"""

_CONVERSE_TURN = """Conversation emploi — {name} | Tours restants : {turns_remaining}

DÉJÀ CONNU (ne jamais redemander) :
{collected}

HISTORIQUE :
{history}

NOUVEAU MESSAGE : "{text}"

RÈGLES :
- Utilise DÉJÀ CONNU : ne repose aucune question dont la réponse est déjà là.
- Applique la GESTION DE L'INATTENDU du prompt système (hors-sujet, flou, vide, multi-infos, contradiction).
- Extrais uniquement ce que le nouveau message apporte (schéma strict, null sinon).
- Offreur → confirmation courte → done=true.
- Tours restants = 1 → groupe toutes les questions manquantes.
- Tours restants = 0 → done=true, confirmation courte avec ce qu'on a.
- JAMAIS de salutation. JAMAIS de promesse.

FORMAT (JSON STRICT) :
{{ "message": "...", "extracted": {{...}}, "done": false }}"""


_SECTEURS_CONNUS = {
    "informatique", "tech", "finance", "comptabilité", "marketing", "communication",
    "santé", "éducation", "btp", "ingénierie", "droit", "juridique",
    "livraison", "manutention", "vente", "nettoyage", "gardiennage", "bricolage",
    "déménagement", "restauration", "événementiel", "petits jobs", "missions courtes",
}

_NIVEAUX_VALIDES = {"aucun", "bac", "bac+2", "bac+3", "bac+5", "doctorat"}
_CONTRATS_VALIDES = {"cdi", "cdd", "stage", "freelance", "mission_courte", "indifferent"}
_EMPLOI_TYPES_VALIDES = {"petit_job", "entreprise", "les_deux"}
_INTENTS_VALIDES = {"demandeur", "offreur", "les_deux"}

# Mots qui ne peuvent pas être une localisation (probablement une confusion LLM)
_NON_LIEUX = {
    "livraison", "manutention", "vente", "nettoyage", "gardiennage", "informatique",
    "finance", "marketing", "santé", "droit", "bac", "master", "doctorat",
    "cdi", "cdd", "stage", "freelance", "emploi", "travail", "boulot",
}


def _validate_champ(field: str, val) -> bool:
    """
    Valide une valeur extraite par le LLM avant de la stocker dans la fiche.
    Retourne False si la valeur est incohérente → champ ignoré, fiche non polluée.
    """
    if not isinstance(val, (str, bool, int)):
        return False

    if isinstance(val, str):
        val_low = val.strip().lower()
        if not val_low:
            return False

        if field == "localisation":
            # Trop long = probablement une phrase, pas une ville
            if len(val) > 60:
                return False
            # Mot-clé qui ne peut pas être une ville
            if any(mot in val_low for mot in _NON_LIEUX):
                return False

        elif field == "niveau_etudes":
            # Normalise et vérifie
            normalized = val_low.replace("é", "e").replace("è", "e")
            return any(n in normalized for n in _NIVEAUX_VALIDES)

        elif field == "type_contrat":
            return val_low in _CONTRATS_VALIDES

        elif field == "emploi_type":
            return val_low in _EMPLOI_TYPES_VALIDES

        elif field in ("intent", "role"):
            return val_low in _INTENTS_VALIDES

        elif field == "secteur_emploi":
            # Trop long = probablement une phrase entière, pas un secteur
            if len(val) > 80:
                return False

    return True


async def converse_emploi(
    user_message: str | None,
    user_name: str,
    pays: str,
    conversation_state: dict,
    is_emploi_only: bool = False,
) -> dict:
    """
    Gère un tour de la conversation libre onboarding emploi.

    Retourne :
      {
        "message"          : str,
        "collected"        : dict,
        "done"             : bool,
        "needs_cv"         : bool,
        "intent"           : "demandeur" | "offreur" | "les_deux" | None,
        "intent_confirmed" : bool,   # True = offreur a confirmé → lancer post_job
        "turns"            : int,
      }
    """
    history: list = conversation_state.get("history", [])
    collected: dict = conversation_state.get("collected", {})
    turns: int = conversation_state.get("turns", 0)

    MAX_TURNS = 4

    # Premier message → opener
    if not history and user_message is None:
        prompt = _CONVERSE_OPENER.format(
            name=user_name, pays=pays or "Sénégal", max_turns=MAX_TURNS
        )
    else:
        turns_remaining = max(0, MAX_TURNS - turns)
        collected_str = "\n".join(
            f"  {k}: {v}" for k, v in collected.items() if v is not None
        ) or "  (rien encore)"
        history_str = "\n".join(
            f"  {m['role'].upper()}: {m['content']}" for m in history[-6:]
        ) or "  (début)"
        prompt = _CONVERSE_TURN.format(
            name=user_name,
            turns_remaining=turns_remaining,
            collected=collected_str,
            history=history_str,
            text=user_message or "",
        )

    result = await _call_llm_json(prompt, system=_CONVERSE_SYSTEM)

    # Fallback codé en dur si LLM échoue — le flow ne s'arrête jamais
    if not result or not result.get("message"):
        fallback_msg = (
            f"{user_name}, tu cherches du travail ou tu as un job à proposer ?"
            if turns == 0
            else "Dis-moi ton domaine et ta ville, je m'occupe du reste."
        )
        return {
            "message": fallback_msg,
            "collected": collected,
            "done": turns >= MAX_TURNS,
            "needs_cv": False,
            "intent": collected.get("intent"),
            "intent_confirmed": False,
            "turns": turns + 1,
        }

    # Merge des champs extraits — validation + mapping role→intent
    new_extracted = result.get("extracted") or {}
    # Schéma LLM utilise "role" ; en interne on stocke "intent"
    if "role" in new_extracted and new_extracted["role"] is not None:
        new_extracted["intent"] = new_extracted.pop("role")
    else:
        new_extracted.pop("role", None)
    for field, val in new_extracted.items():
        if val is not None and _validate_champ(field, val):
            collected[field] = val

    done = bool(result.get("done", False))

    # intent_confirmed : déduit en code (offreur + done → confirmé)
    intent = collected.get("intent")
    intent_confirmed = bool(
        collected.get("intent_confirmed", False)
        or (done and intent == "offreur")
    )
    if intent_confirmed:
        collected["intent_confirmed"] = True

    # needs_cv : déduit en code depuis emploi_type, jamais depuis LLM
    _et = collected.get("emploi_type")
    needs_cv = _et in ("entreprise", "les_deux")

    turns += 1
    if turns >= MAX_TURNS:
        done = True

    return {
        "message": result["message"],
        "collected": collected,
        "done": done,
        "needs_cv": needs_cv,
        "intent": intent,
        "intent_confirmed": intent_confirmed,
        "turns": turns,
    }


# ─── Helper : faut-il activer le LLM pour cette étape ? ──────────────────────

def should_analyze(step: str, text: str) -> bool:
    """
    Retourne True si on doit passer par le LLM pour cette étape.
    On skip le LLM si :
    - L'étape n'est pas dans STEP_SPECS
    - Le texte ressemble déjà à une réponse bouton structurée (id technique)
    """
    if step not in STEP_SPECS:
        return False
    if not text or not text.strip():
        return False
    # Les IDs de boutons WhatsApp (ex: "usage_etudes", "pays_oui") sont déjà propres
    # On les reconnaît car ils sont en snake_case sans espaces
    if re.match(r'^[a-z][a-z0-9_]{2,}$', text.strip()):
        return False
    return True
