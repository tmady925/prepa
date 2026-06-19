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
from app.services import qualification

settings = get_settings()

# Longueur dure d'une réponse du routeur (anti-pavé). Au-delà → tronqué.
_REPLY_MAX_CHARS = 280


# ─── Enums de validation (source de vérité côté code) ────────────────────────

_NIVEAUX_VALIDES = {"aucun", "bac", "bac+2", "bac+3", "bac+5", "doctorat"}
_CONTRATS_VALIDES = {"cdi", "cdd", "stage", "freelance", "indifferent"}
_ACTIONS_VALIDES = {
    "none", "show_jobs", "show_petit_jobs", "show_profile", "show_plan",
    "post_job", "show_invite", "show_menu",
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

_CAPABILITIES = """Tu es Prepa, un assistant emploi WhatsApp pour les jeunes en Afrique de l'Ouest.

⚠️ TU N'ES PAS UN CHATBOT LIBRE. Tu es un OUTIL GUIDÉ. Ton seul rôle est de COMPRENDRE
le message et de le ROUTER vers un SERVICE du bot. Tu ne rédiges JAMAIS de conseils,
de cours, ni de longs textes.

LES SEULS SERVICES DU BOT (tu routes vers l'un d'eux) :
- "show_jobs"        → voir les offres d'emploi (entreprise) qui matchent son profil
- "show_petit_jobs"  → voir les petits jobs / missions courtes
- "show_profile"     → voir ou modifier son profil
- "show_plan"        → infos sur le plan (Gratuit / Pro)
- "show_invite"      → inviter des amis
- "post_job"         → un RECRUTEUR veut publier une mission
- "show_menu"        → réafficher le menu (cas par défaut quand rien d'autre ne colle)

CE QUE TU NE FAIS JAMAIS :
- AUCUN coaching, AUCUN conseil rédigé (CV, lettre, entretien, carrière). Ces services
  n'existent PAS encore → action "show_menu" avec une phrase « pas encore disponible ».
- JAMAIS d'études / examens scolaires (BAC, BFEM) / concours → "show_menu", recentre emploi.
- Tu n'inventes JAMAIS une offre, un salaire, une entreprise, une date, un nombre.
- Tu ne promets JAMAIS un emploi ni un résultat."""

_SYSTEM_PROMPT = _CAPABILITIES + """

━━━ TON TRAVAIL À CHAQUE MESSAGE ━━━
1. Déduis l'INTENTION (candidat / recruteur / inconnu) — sans demander frontalement.
2. EXTRAIS uniquement les infos profil présentes dans CE message (rien si déjà dans PROFIL CONNU).
3. Détecte une CORRECTION et choisis UN service (ou "show_menu").

━━━ CORRECTIONS (très important) ━━━
Si l'utilisateur se corrige (« je me suis trompé », « en fait c'est… », « plutôt… »,
« non, change… », « finalement… ») → mets la NOUVELLE valeur dans "profile" (elle écrase
l'ancienne). Reformule la valeur corrigée, pas l'ancienne.

━━━ INTENTION (signaux) ━━━
RECRUTEUR → "post_job" : « je recrute », « j'ai besoin d'un(e) [métier] », « besoin d'un(e) … »,
  « je cherche quelqu'un pour… », « pour ma maison / mon entreprise », « postes à pourvoir »,
  « je propose / publie une offre ». Ex : « besoin d'un gardien à Pikine » → recruteur.
CANDIDAT : « je cherche un emploi / stage », « je suis [métier] et je cherche », « je peux faire… ».
Ambigu → intent "inconnu" + "show_menu".
Politesse / bruit (« ok », « merci », « hmm ») → action "show_menu", PAS d'offres.

━━━ TON (strict) ━━━
- "reply" = UNE phrase courte MAXIMUM (confirmation, mini-question, ou redirection). JAMAIS un pavé.
- PAS de salutation (« Bonjour / Salut »), jamais.
- Si tu as assez d'infos → SERS (action de service) au lieu de questionner.
- "show_profile" seulement s'il demande explicitement son profil ; sinon ne l'affiche pas vide.

━━━ EXTRACTION (null si ABSENT, n'invente jamais) ━━━
- secteur        : un ou PLUSIEURS domaines cités, séparés par des virgules
                   (ex: "informatique" ; "comptabilité, vente") ou null
- niveau_etudes  : niveau cité (bac, bac+2, BTS, licence, master, BFEM, CAP…) ou null
- localisation   : ville/quartier UNIQUEMENT si présent dans le message, sinon null
- type_contrat   : un de [cdi, cdd, stage, freelance, indifferent] ou null

━━━ FORMAT DE SORTIE (JSON STRICT, rien d'autre) ━━━
{
  "intent": "candidat" | "recruteur" | "inconnu",
  "reply": "<UNE phrase courte, ou null>",
  "action": "show_jobs" | "show_petit_jobs" | "show_profile" | "show_plan" | "show_invite" | "post_job" | "show_menu" | "none",
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

    # secteur → secteur_emploi (LISTE, multi-secteur). On découpe sur virgules / « et ».
    secteur = raw.get("secteur")
    if isinstance(secteur, str) and secteur.strip():
        parts = re.split(r"[,;/]|\bet\b", secteur)
        secteurs = []
        for p in parts:
            s = p.strip()
            if 1 < len(s) <= 40 and _norm(s) not in _NIVEAUX_VALIDES | _CONTRATS_VALIDES:
                secteurs.append(s)
        if secteurs:
            out["secteur_emploi"] = secteurs

    # niveau → canonicalisé via l'échelle qualification (BTS→bac+2, licence→bac+3…).
    niveau = raw.get("niveau_etudes")
    if isinstance(niveau, str) and niveau.strip():
        canon, rank = qualification.normalize_niveau(niveau)
        if canon:
            out["niveau_etudes"] = canon

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
        if not isinstance(reply, str) or not reply.strip():
            reply = None
        else:
            reply = reply.strip()
            # Cap DUR anti-pavé : le routeur ne « vit » pas, il oriente.
            if len(reply) > _REPLY_MAX_CHARS:
                reply = reply[:_REPLY_MAX_CHARS].rstrip() + "…"

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
