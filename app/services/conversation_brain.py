"""
Conversation Brain — cerveau conversationnel emploi (post-onboarding)
=====================================================================

PRINCIPE FONDATEUR : « le LLM propose, le code dispose ».
- Le LLM *décide* (intention + action) et *extrait* (champs profil) en JSON strict.
- Le *code* valide chaque champ contre des enums, exécute, et rend les faits réels.
- Une erreur du LLM ne peut donc produire, au pire, qu'une phrase maladroite ou une
  question en trop — jamais une fausse offre, jamais un champ invalide en base, jamais
  un plantage.

GARDE-FOUS :
- Anti-invention : « si l'info n'est PAS dans le message → null » (consigne vérifiée
  empiriquement : supprime les hallucinations de ville/secteur).
- Validation par enums côté code : un secteur/niveau/contrat incohérent est ignoré.
- Aucune confiance dans le champ `confidence` du LLM (il ment — toujours ~0.95).
- Chaîne de repli : Mistral small → Mistral large → Groq → None. `None` ⇒ le webhook
  applique son propre fallback (answer_emploi puis message générique). Jamais de cul-de-sac.

PROVIDER : Mistral en premier (cf. décision projet), Groq en dernier recours.

ENTRÉE :
    await brain_decide(text, user, db, history) -> dict | None

RETOUR NORMALISÉ (dict) :
    {
      "intent":  "candidat" | "recruteur" | "inconnu",
      "reply":   str | None,        # message humain à envoyer (ou None si une action suffit)
      "action":  "none" | "show_jobs" | "show_petit_jobs" | "show_profile"
                 | "show_plan" | "post_job",
      "profile_updates": {          # champs déjà validés, prêts à persister (ou {})
          "secteur_emploi":        list[str],   # optionnel
          "niveau_etudes":         str,         # optionnel
          "localisation_emploi":   str,         # optionnel
          "type_contrat_souhaite": str,         # optionnel
      },
    }
    None si le LLM échoue totalement.
"""

import json
import re
from typing import Any

import httpx

from app.core.settings import get_settings

settings = get_settings()


# ─── Enums de validation (source de vérité côté code) ────────────────────────

_NIVEAUX_VALIDES = {"aucun", "bac", "bac+2", "bac+3", "bac+5", "doctorat"}
_CONTRATS_VALIDES = {"cdi", "cdd", "stage", "freelance", "indifferent"}
_ACTIONS_VALIDES = {
    "none", "show_jobs", "show_petit_jobs", "show_profile", "show_plan", "post_job",
}
_INTENTS_VALIDES = {"candidat", "recruteur", "inconnu"}

# Mots qui ne PEUVENT pas être une localisation (hallucination probable du LLM).
_NON_LIEUX = {
    "livraison", "manutention", "vente", "nettoyage", "gardiennage", "informatique",
    "finance", "marketing", "sante", "santé", "droit", "bac", "master", "doctorat",
    "cdi", "cdd", "stage", "freelance", "emploi", "travail", "boulot", "comptabilite",
    "comptabilité", "rh", "communication",
}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("é", "e").replace("è", "e").replace("ê", "e")


# ─── Capacités du bot (le bot « comprend son propre fonctionnement ») ─────────

_CAPABILITIES = """Tu es Prepa, l'assistant emploi WhatsApp pour les jeunes en Afrique de l'Ouest.

CE QUE TU SAIS FAIRE (et rien d'autre) :
- Montrer à l'utilisateur les offres d'emploi qui matchent son profil  → action "show_jobs"
- Montrer les petits jobs / missions courtes disponibles               → action "show_petit_jobs"
- Montrer / faire modifier son profil                                  → action "show_profile"
- Donner les infos sur son plan (Gratuit / Pro)                        → action "show_plan"
- Aider un recruteur à publier une mission                             → action "post_job"
- Conseiller : CV, lettre de motivation, entretien, carrière           → action "none" + reply
- Compléter son profil au fil de la conversation (secteur, niveau, ville, contrat)

CE QUE TU NE FAIS JAMAIS :
- Tu ne parles JAMAIS d'études, d'examens scolaires (BAC, BFEM) ni de concours.
  Si l'utilisateur en parle, recentre-le gentiment sur l'emploi.
- Tu n'inventes JAMAIS une offre, un salaire, une entreprise, une date ou un nombre.
  Les offres sont affichées par le système, pas par toi.
- Tu ne promets JAMAIS un emploi, un résultat ni un délai. Tu connectes, tu ne garantis rien."""

_SYSTEM_PROMPT = _CAPABILITIES + """

━━━ TON RÔLE À CHAQUE MESSAGE ━━━
1. Déduis l'INTENTION (candidat / recruteur / inconnu) — sans jamais demander
   frontalement « tu cherches ou tu recrutes » si c'est déduisible.
2. EXTRAIS du MESSAGE uniquement les infos profil RÉELLEMENT présentes
   (n'extrais PAS ce qui est déjà dans PROFIL CONNU — seulement ce que le message AJOUTE).
3. CHOISIS une action, ou pose UNE seule question courte si une info essentielle manque.

━━━ DÉTECTION DE L'INTENTION (signaux explicites) ━━━
RECRUTEUR (il propose du travail / cherche de la main-d'œuvre) → action "post_job" :
  « je recrute », « j'ai besoin d'un(e) [métier] », « besoin d'un(e) [métier] »,
  « je cherche quelqu'un / une personne pour… », « pour ma maison / mon entreprise / mon resto »,
  « X postes à pourvoir », « j'offre / je propose un job », « je veux publier / poster une offre ».
  Ex : « besoin d'un gardien de nuit à Pikine » → recruteur (il EMBAUCHE un gardien).
CANDIDAT (il cherche du travail pour lui-même) :
  « je cherche un emploi / du travail / un stage », « je suis [métier] et je cherche »,
  « jcherche du boulot », « disponible pour… », « je peux faire… ».
Si vraiment ambigu après lecture → intent "inconnu" + UNE question courte pour confirmer
(ne devine pas au hasard).
Politesse pure / bruit (« ok », « merci », « hmm ») → action "none", ne montre PAS d'offres.

━━━ RÈGLES DE TON (anti-robot) ━━━
- Phrases courtes. 2 phrases max. Une seule question à la fois.
- PAS de salutation (« Bonjour / Salut ») — jamais, même en réponse à « salut ».
- Naturel, chaleureux, direct. Pas de pavé, pas de réponse figée.
- Si tu as assez d'infos pour servir, SERS (montre les offres) au lieu de continuer à questionner.
- Profil vide + l'utilisateur cherche un emploi → "show_jobs" ou demande son secteur en 1 question.
  N'affiche JAMAIS un profil vide ("show_profile" UNIQUEMENT s'il demande explicitement à voir/modifier son profil).
- Demande de conseil (CV, entretien, lettre) → donne 1 conseil concret en 1 phrase, puis propose
  d'aller plus loin. Ne réponds pas QUE par une question.

━━━ EXTRACTION (schéma STRICT — null si ABSENT du message, n'invente jamais) ━━━
- secteur        : domaine cité (ex: "informatique", "comptabilité", "livraison") ou null
- niveau_etudes  : un de [aucun, bac, bac+2, bac+3, bac+5, doctorat] ou null
                   (mappe : BTS→bac+2, licence→bac+3, master/ingénieur→bac+5, phd→doctorat)
- localisation   : ville/quartier UNIQUEMENT si présent dans le message, sinon null
- type_contrat   : un de [cdi, cdd, stage, freelance, indifferent] ou null

━━━ FORMAT DE SORTIE (JSON STRICT, rien d'autre) ━━━
{
  "intent": "candidat" | "recruteur" | "inconnu",
  "reply": "<message court à envoyer, ou null si une action affiche déjà le résultat>",
  "action": "none" | "show_jobs" | "show_petit_jobs" | "show_profile" | "show_plan" | "post_job",
  "profile": {
    "secteur": <string|null>,
    "niveau_etudes": <string|null>,
    "localisation": <string|null>,
    "type_contrat": <string|null>
  }
}"""


# ─── Appel LLM JSON (Mistral d'abord, Groq en dernier) ───────────────────────

def _providers() -> list[tuple]:
    provs = []
    if getattr(settings, "mistral_api_key", None):
        provs.append(("mistral-small", "https://api.mistral.ai/v1/chat/completions",
                      "mistral-small-latest", settings.mistral_api_key))
        provs.append(("mistral-large", "https://api.mistral.ai/v1/chat/completions",
                      "mistral-large-latest", settings.mistral_api_key))
    if getattr(settings, "groq_api_key", None):
        provs.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                      "llama-3.3-70b-versatile", settings.groq_api_key))
    return provs


async def _call_json(system: str, user_prompt: str) -> dict | None:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    for name, url, model, key in _providers():
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "max_tokens": 400,
                        "temperature": 0.2,
                    },
                )
                if resp.status_code != 200:
                    print(f"  [conversation_brain] {name} HTTP {resp.status_code}")
                    continue
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
                result = json.loads(raw)
                print(f"  [conversation_brain] provider={name} "
                      f"intent={result.get('intent')} action={result.get('action')}")
                return result
        except Exception as e:
            print(f"  [conversation_brain] provider {name} error: {e}")
            continue
    return None


# ─── Validation des champs extraits ──────────────────────────────────────────

def _validate_profile(raw: dict | None) -> dict:
    """
    Filtre les champs extraits par le LLM contre les enums.
    Retourne uniquement les valeurs SAINES, sous les clés du modèle User.
    Une valeur incohérente est silencieusement ignorée (fiche jamais polluée).
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out

    # secteur → secteur_emploi (liste). On accepte un libellé court et sensé.
    secteur = raw.get("secteur")
    if isinstance(secteur, str):
        s = secteur.strip()
        if 1 < len(s) <= 60 and _norm(s) not in _NIVEAUX_VALIDES | _CONTRATS_VALIDES:
            out["secteur_emploi"] = [s]

    niveau = raw.get("niveau_etudes")
    if isinstance(niveau, str):
        n = _norm(niveau)
        if n in _NIVEAUX_VALIDES:
            out["niveau_etudes"] = n

    loc = raw.get("localisation")
    if isinstance(loc, str):
        l = loc.strip()
        if 1 < len(l) <= 60 and not any(m in _norm(l) for m in _NON_LIEUX):
            out["localisation_emploi"] = l

    contrat = raw.get("type_contrat")
    if isinstance(contrat, str):
        c = _norm(contrat)
        if c in _CONTRATS_VALIDES:
            out["type_contrat_souhaite"] = c

    return out


# ─── Contexte utilisateur injecté à chaque tour (source de vérité = DB) ───────

async def _build_context(user, db) -> str:
    parts = [
        "PROFIL CONNU (ne redemande jamais ce qui est déjà rempli) :",
        f"- Prénom : {getattr(user, 'name', None) or 'inconnu'}",
        f"- Plan : {getattr(user, 'plan', None) or 'free'}",
        f"- Secteur(s) : {', '.join(getattr(user, 'secteur_emploi', None) or []) or 'non renseigné'}",
        f"- Niveau d'études : {getattr(user, 'niveau_etudes', None) or 'non renseigné'}",
        f"- Localisation : {getattr(user, 'localisation_emploi', None) or 'non renseignée'}",
        f"- Type de contrat : {getattr(user, 'type_contrat_souhaite', None) or 'non renseigné'}",
    ]

    # Le contexte profil ci-dessus ne dépend PAS de la DB. Le compte d'offres,
    # lui, en a besoin : on le saute proprement si db absente.
    if db is None:
        return "\n".join(parts)

    # Nombre d'offres matchées (pour que le LLM sache s'il peut en proposer),
    # SANS jamais lui donner le détail (le détail est rendu par le code).
    try:
        from sqlalchemy import select, func as safunc
        from app.models.candidate_profile import JobMatch
        from app.models.job_opportunity import JobOpportunity
        nb = await db.scalar(
            select(safunc.count(JobMatch.id))
            .join(JobOpportunity, JobMatch.job_id == JobOpportunity.id)
            .where(JobMatch.user_id == user.id, JobOpportunity.statut == "active")
        )
        parts.append(f"- Offres matchées disponibles : {nb or 0}")
    except Exception as e:
        print(f"  [conversation_brain] context match-count error: {e}")

    return "\n".join(parts)


# ─── Fonction principale ──────────────────────────────────────────────────────

async def brain_decide(text: str, user, db=None, history: list | None = None) -> dict | None:
    """
    Décide quoi faire d'un message texte libre (post-onboarding).
    Pure décision : n'écrit rien en base, n'envoie aucun message.
    Retourne le dict normalisé (cf. docstring module) ou None si échec total.
    """
    if not text or len(text.strip()) < 2:
        return None

    try:
        context = await _build_context(user, db)

        history_str = ""
        if history:
            recent = history[-6:]
            lines = []
            for m in recent:
                role = "Utilisateur" if m.get("role") == "user" else "Prepa"
                lines.append(f"{role}: {(m.get('content') or '')[:200]}")
            history_str = "\nHISTORIQUE RÉCENT :\n" + "\n".join(lines)

        prompt = f"""{context}
{history_str}

MESSAGE REÇU : "{text}"

Analyse-le et retourne le JSON de décision (schéma strict du system prompt).
Rappel : null pour tout champ profil ABSENT du message. N'invente rien."""

        result = await _call_json(_SYSTEM_PROMPT, prompt)
        if not isinstance(result, dict):
            return None

        intent = result.get("intent")
        if intent not in _INTENTS_VALIDES:
            intent = "inconnu"

        action = result.get("action")
        if action not in _ACTIONS_VALIDES:
            action = "none"

        reply = result.get("reply")
        if reply is not None and not isinstance(reply, str):
            reply = None
        if isinstance(reply, str) and not reply.strip():
            reply = None

        profile_updates = _validate_profile(result.get("profile"))

        # Garde-fou : pour un recruteur, les champs extraits décrivent le JOB
        # proposé, pas son profil de candidat → on ne pollue pas sa fiche.
        if intent == "recruteur":
            profile_updates = {}

        # Garde-fou : si aucune action ET aucun message ET rien à enregistrer,
        # on renvoie None pour laisser le fallback du webhook répondre.
        if action == "none" and not reply and not profile_updates:
            return None

        return {
            "intent": intent,
            "reply": reply,
            "action": action,
            "profile_updates": profile_updates,
        }

    except Exception as e:
        print(f"  [conversation_brain] brain_decide error: {e}")
        return None


# ─── Persistance des champs profil + re-matching ─────────────────────────────

async def apply_profile_updates(db, user, updates: dict) -> bool:
    """
    Applique les champs validés au User ET au CandidateProfile, régénère
    l'embedding et relance le matching si quelque chose a changé.
    Retourne True si le profil a effectivement été modifié.

    Tolérant aux erreurs : une exception ne casse jamais la conversation.
    """
    if not updates:
        return False

    changed = False
    try:
        # ── secteur_emploi : fusion sans doublon ──────────────────────────
        new_secteurs = updates.get("secteur_emploi")
        if new_secteurs:
            current = list(getattr(user, "secteur_emploi", None) or [])
            for s in new_secteurs:
                if s and s not in current:
                    current.append(s)
                    changed = True
            if changed:
                user.secteur_emploi = current

        for field in ("niveau_etudes", "localisation_emploi", "type_contrat_souhaite"):
            val = updates.get(field)
            if val and getattr(user, field, None) != val:
                setattr(user, field, val)
                changed = True

        if not changed:
            return False

        # ── Miroir sur CandidateProfile (créé si absent) ──────────────────
        from sqlalchemy import select
        from app.models.candidate_profile import CandidateProfile
        cp = (await db.execute(
            select(CandidateProfile).where(CandidateProfile.user_id == user.id)
        )).scalar_one_or_none()
        if cp is None:
            cp = CandidateProfile(user_id=user.id)
            db.add(cp)

        if user.secteur_emploi:
            cp.secteurs_interets = list(user.secteur_emploi)
        if user.niveau_etudes:
            cp.niveau_etudes = user.niveau_etudes
        if user.localisation_emploi:
            cp.localisation = user.localisation_emploi
        if user.type_contrat_souhaite:
            cp.type_contrat_souhaite = user.type_contrat_souhaite

        # ── Régénère l'embedding profil (matching sémantique) ─────────────
        try:
            from app.services.rag.embedding_service import embedding_service
            embed_text = " ".join(filter(None, [
                ", ".join(cp.secteurs_interets or []),
                cp.niveau_etudes or "",
                cp.localisation or "",
                cp.type_contrat_souhaite or "",
            ])).strip()
            if embed_text:
                emb = await embedding_service.embed_text(embed_text)
                if emb:
                    cp.embedding = emb
        except Exception as e:
            print(f"  [conversation_brain] embedding regen error: {e}")

        await db.flush()
        return True

    except Exception as e:
        print(f"  [conversation_brain] apply_profile_updates error: {e}")
        return False


async def rematch(db, user) -> int:
    """
    Relance le matching pour ce candidat après mise à jour du profil.
    match_candidate envoie lui-même les notifications (quota géré).
    Retourne le nombre de matches, 0 si erreur (jamais d'exception propagée).
    """
    try:
        from app.services.matching_service import matching_service
        matches = await matching_service.match_candidate(db, user.id)
        return len(matches or [])
    except Exception as e:
        print(f"  [conversation_brain] rematch error: {e}")
        return 0
