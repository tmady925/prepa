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
            "Mapper vers : Informatique/Tech, Finance/Comptabilité, Marketing/Communication, Santé, Éducation, BTP/Ingénierie, Droit/Juridique",
            "Accepter langage naturel : 'banque' → Finance, 'développeur' → Informatique/Tech, 'avocat' → Droit/Juridique",
            "Accepter 'peu importe', 'tout' → garder le texte tel quel",
            "Si l'utilisateur exprime une confusion sur ce qu'il veut faire → action=guide pour l'aider à choisir",
        ],
        "platform_rules": [
            "Le secteur choisi sert au matching avec les offres d'emploi disponibles",
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

SYSTEM_PROMPT = """Tu es l'assistant intelligent de Prepa, une plateforme WhatsApp d'accompagnement pour les jeunes en Afrique.

Prepa couvre 3 domaines :
1. ÉTUDES : révisions BAC/BFEM, exercices, corrections, matières scolaires
2. CONCOURS : préparation aux concours (fonction publique, grandes écoles, privé)
3. EMPLOI : matching offres d'emploi, conseils CV, orientation professionnelle

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


# ─── Appel LLM léger ─────────────────────────────────────────────────────────

async def _call_llm_json(prompt: str) -> dict | None:
    """Appel LLM minimal pour obtenir un JSON. Essaie Groq puis Mistral."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
        result = await _call_llm_json(prompt)
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
