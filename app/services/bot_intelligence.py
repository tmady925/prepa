"""
Bot Intelligence Layer — Couche LLM emploi uniquement
======================================================

PRINCIPE :
- Le LLM reçoit un contexte complet : profil user, offres matchées, historique récent.
- Il analyse l'intention et retourne une décision structurée (JSON).
- Le webhook utilise cette décision pour agir ou laisser passer au flow normal.

INTÉGRATION SÉCURISÉE :
- Toute exception → retourne None → le webhook continue comme avant
- N'est appelé que pour les messages texte non structurés (pas les IDs boutons,
  pas les commandes /, pas les états actifs)

ACTIONS RETOURNÉES :
  "answer"        → Répondre directement avec le message fourni
  "show_jobs"     → Afficher les offres d'emploi matchées
  "show_profile"  → Afficher le profil (/profil)
  "show_plan"     → Afficher les infos plan (/plan)
  "guide_emploi"  → Conseiller sur le CV / l'emploi
  "post_job"      → Publier un petit job (employeur)
  "show_petit_jobs" → Voir les petits jobs disponibles (candidat)
  "passthrough"   → Laisser le flow normal gérer
"""

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from app.core.settings import get_settings

settings = get_settings()

# ─── Règles plateforme injectées dans chaque prompt ──────────────────────────

PLATFORM_RULES = PLATFORM_RULES_EMPLOI  # alias — plateforme emploi uniquement

# ─── Variante EMPLOI UNIQUEMENT ──────────────────────────────────────────────

PLATFORM_RULES_EMPLOI = """
RÈGLES DE LA PLATEFORME PREPA (MODE EMPLOI) :

Prepa est une plateforme dédiée à l'EMPLOI et à la carrière.

PLANS :
- Plan Gratuit : accès limité aux offres
- Plan Pro (500 FCFA/mois) : offres prioritaires, accompagnement complet

SERVICES EMPLOI :
- Matching avec des offres d'emploi selon le profil (secteur, niveau, localisation, contrat)
- Conseils CV et lettre de motivation
- Préparation aux entretiens d'embauche
- Orientation professionnelle

COMMANDES DISPONIBLES :
- /profil → voir et modifier son profil emploi
- /plan → voir/changer son plan
- /aide → liste des commandes
- mes offres → voir les offres matchées

IMPORTANT : Cette plateforme ne traite PAS les études, examens scolaires (BAC, BFEM)
ni les concours. Tu n'en parles jamais. Si l'utilisateur évoque ces sujets,
tu le recentres avec bienveillance sur sa recherche d'emploi.
"""

SYSTEM_PROMPT_EMPLOI = """Tu es Prepa, un assistant emploi intelligent pour les jeunes en Afrique.
Tu accompagnes les utilisateurs UNIQUEMENT sur leur recherche d'emploi et leur carrière.

Tu dois analyser le message de l'utilisateur et retourner un JSON de décision.
Tu réponds TOUJOURS en JSON valide et rien d'autre.

FORMAT DE RÉPONSE :
{
  "action": "<action>",
  "message": "<message WhatsApp formaté ou null>",
  "confidence": 0.0
}

ACTIONS DISPONIBLES :
- "answer"          → Tu peux répondre directement (conseil emploi, CV, entretien, carrière)
- "show_jobs"       → L'utilisateur veut voir ses offres d'emploi
- "show_profile"    → L'utilisateur veut voir son profil
- "show_plan"       → L'utilisateur veut des infos sur son plan/abonnement
- "guide_emploi"    → Conseiller sur l'emploi/CV/entretien
- "post_job"        → L'utilisateur veut proposer/publier un petit job (employeur)
- "show_petit_jobs" → L'utilisateur cherche des petits jobs disponibles (candidat)
- "passthrough"     → Cas très ambigu uniquement

RÈGLES :
- Tu ne proposes JAMAIS d'exercice, d'examen ou de concours (action "exercise" interdite)
- Si l'utilisateur parle d'études/examens/concours → "answer" en le recentrant gentiment sur l'emploi
- Si l'utilisateur demande ses offres → "show_jobs"
- Si l'utilisateur veut proposer un travail ponctuel / petit boulot / mission courte → "post_job"
- Si l'utilisateur cherche un petit job / boulot temporaire / mission → "show_petit_jobs"
- Pour tout conseil carrière/CV/entretien → "answer" avec une réponse complète et utile
- Ton message doit être formaté pour WhatsApp (gras avec *, listes avec -)
- Sois bienveillant, chaleureux, concret, adapté à un jeune africain
- confidence entre 0.0 et 1.0

ROUTING EMPLOI INTELLIGENT (utilise PRÉFÉRENCE EMPLOI si disponible dans le contexte) :
- PRÉFÉRENCE "Petits jobs"         → orienter d'abord vers show_petit_jobs avant show_jobs
- PRÉFÉRENCE "Offres d'entreprise" → orienter vers show_jobs ; ne pas proposer show_petit_jobs sauf si demande explicite
- PRÉFÉRENCE "Les deux"            → proposer selon la nature de la demande
- Signaux urgence ("vite", "urgent", "disponible maintenant") → show_petit_jobs
- Signaux carrière ("CDI", "poste", "long terme") → show_jobs
"""

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = SYSTEM_PROMPT_EMPLOI  # alias — plateforme emploi uniquement


# ─── Constructeur de contexte ─────────────────────────────────────────────────

async def build_user_context(user, db=None) -> str:
    """
    Construit un contexte riche sur l'utilisateur pour le LLM.
    Inclut : profil emploi, offres matchées.
    Chaque section est gérée indépendamment — une erreur n'empêche pas les autres.
    """
    ctx_parts = []

    # ── Profil de base ────────────────────────────────────────────────────────
    try:
        ctx_parts.append(f"""PROFIL UTILISATEUR :
- Prénom : {user.name or 'inconnu'}
- Pays : {getattr(user, 'pays', None) or 'non renseigné'}
- Plan : {user.plan or 'free'}
- Niveau d'études : {getattr(user, 'niveau_etudes', None) or 'non défini'}
- Secteur visé : {', '.join(getattr(user, 'secteur_emploi', None) or []) or 'non défini'}
- Localisation : {getattr(user, 'localisation_emploi', None) or 'non définie'}
- Type de contrat : {getattr(user, 'type_contrat_souhaite', None) or 'non défini'}""")
    except Exception as e:
        print(f"  [bot_intelligence] profil error: {e}")

    # ── Quota ────────────────────────────────────────────────────────────────
    try:
        daily_used = getattr(user, 'daily_messages_used', 0) or 0
        plan = user.plan or 'free'
        if plan == 'free':
            ctx_parts.append(f"QUOTA : {daily_used}/10 messages utilisés aujourd'hui")
        else:
            ctx_parts.append(f"QUOTA : Plan Pro — pas de limite journalière")
    except Exception:
        pass

    # ── Type d'emploi préféré ─────────────────────────────────────────────────
    if db:
        try:
            from sqlalchemy import select as _sel_cp
            from app.models.candidate_profile import CandidateProfile as _CP
            _cp_res = await db.execute(
                _sel_cp(_CP).where(_CP.user_id == user.id)
            )
            _cp = _cp_res.scalar_one_or_none()
            if _cp and _cp.emploi_type:
                _label = {
                    "petit_job":  "Petits jobs / missions ponctuelles",
                    "entreprise": "Offres d'entreprise (CDI/CDD/Stage)",
                    "les_deux":   "Les deux (petits jobs et offres d'entreprise)",
                }.get(_cp.emploi_type, _cp.emploi_type)
                ctx_parts.append(f"PRÉFÉRENCE EMPLOI : {_label}")
        except Exception as e:
            print(f"  [bot_intelligence] emploi_type context error: {e}")

    # ── Offres d'emploi matchées ──────────────────────────────────────────────
    if db:
        try:
            from sqlalchemy import select
            from app.models.job_match import JobMatch
            from app.models.job_opportunity import JobOpportunity

            result = await db.execute(
                select(JobMatch, JobOpportunity)
                .join(JobOpportunity, JobMatch.job_id == JobOpportunity.id)
                .where(
                    JobMatch.user_id == user.id,
                    JobOpportunity.statut == "active",
                )
                .order_by(JobMatch.score.desc())
                .limit(5)
            )
            rows = result.all()

            if rows:
                jobs_ctx = "OFFRES D'EMPLOI MATCHÉES (les plus récentes) :\n"
                for match, job in rows:
                    deadline = ""
                    if job.date_limite:
                        try:
                            dl = job.date_limite
                            if hasattr(dl, 'strftime'):
                                deadline = f" — deadline {dl.strftime('%d/%m/%Y')}"
                        except Exception:
                            pass
                    jobs_ctx += f"- {job.titre} chez {job.entreprise or 'N/A'}{deadline} (score matching: {match.score:.0f}%)\n"
                ctx_parts.append(jobs_ctx.strip())
            else:
                ctx_parts.append("OFFRES D'EMPLOI : Aucune offre matchée pour l'instant")
        except Exception as e:
            print(f"  [bot_intelligence] jobs context error: {e}")

    return "\n\n".join(ctx_parts)


# ─── Appel LLM ────────────────────────────────────────────────────────────────

async def _call_llm_json(system: str, user_message: str) -> dict | None:
    """Appel LLM léger pour obtenir un JSON de décision."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
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
                    json={"model": model, "messages": messages, "max_tokens": 500, "temperature": 0.2},
                )
                if resp.status_code != 200:
                    continue
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
                result = json.loads(raw)
                print(f"  [bot_intelligence] provider={name} action={result.get('action')} confidence={result.get('confidence')}")
                return result
        except Exception as e:
            print(f"  [bot_intelligence] provider {name} error: {e}")
            continue

    return None


# ─── Fonction principale ──────────────────────────────────────────────────────

async def analyze_message(
    text: str,
    user,
    db=None,
    history: list = None,
) -> dict | None:
    """
    Analyse un message reçu en dehors de l'onboarding.

    Retourne un dict :
      {action, message, confidence}
    ou None si le LLM échoue (→ le webhook continue normalement).

    N'est PAS appelé pour :
    - Les IDs boutons structurés (snake_case)
    - Les commandes /
    - Les états actifs gérés (awaiting_copy...)
    """
    if not text or not text.strip() or len(text.strip()) < 2:
        return None

    try:
        user_context = await build_user_context(user, db)

        history_str = ""
        if history:
            recent = history[-6:] if len(history) > 6 else history
            lines = []
            for msg in recent:
                role = "Utilisateur" if msg.get("role") == "user" else "Prepa"
                lines.append(f"{role}: {msg.get('content', '')[:200]}")
            history_str = "\nHISTORIQUE RÉCENT :\n" + "\n".join(lines)

        _instructions = """Analyse ce message et retourne la décision JSON appropriée.
Si l'utilisateur demande ses offres d'emploi → action="show_jobs".
Pour tout conseil emploi/CV/entretien/carrière → action="answer" avec une réponse complète.
Si l'utilisateur parle d'études/examens/concours → action="answer" en le recentrant gentiment sur l'emploi.
Ne propose JAMAIS d'exercice ni d'examen."""

        prompt = f"""{PLATFORM_RULES_EMPLOI}

{user_context}
{history_str}

DATE ACTUELLE : {datetime.now().strftime('%d/%m/%Y %H:%M')}

MESSAGE REÇU : "{text}"

{_instructions}"""

        result = await _call_llm_json(SYSTEM_PROMPT_EMPLOI, prompt)

        if not result or "action" not in result:
            return None

        action = result.get("action", "passthrough")
        confidence = float(result.get("confidence", 0.5))

        if confidence < 0.5 and action != "passthrough":
            return {"action": "passthrough", "message": None, "confidence": confidence}

        return {
            "action": action,
            "message": result.get("message"),
            "confidence": confidence,
        }

    except Exception as e:
        print(f"  [bot_intelligence] analyze_message error: {e}")
        return None


# ─── Réponse emploi en texte libre (mode emploi uniquement) ──────────────────

async def _call_llm_text(system: str, user_message: str) -> str | None:
    """Appel LLM pour obtenir une réponse texte (pas JSON)."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]
    providers = []
    if getattr(settings, "groq_api_key", None):
        providers.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                          "llama-3.3-70b-versatile", settings.groq_api_key))
    if getattr(settings, "mistral_api_key", None):
        providers.append(("mistral", "https://api.mistral.ai/v1/chat/completions",
                          "mistral-large-latest", settings.mistral_api_key))

    for name, url, model, key in providers:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": 500, "temperature": 0.6},
                )
                if resp.status_code != 200:
                    continue
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  [bot_intelligence] text provider {name} error: {e}")
            continue
    return None


EMPLOI_ASSISTANT_PROMPT = """Tu es Prepa, un assistant emploi chaleureux pour les jeunes en Afrique.
Tu aides UNIQUEMENT sur la recherche d'emploi et la carrière :
- Trouver et comprendre des offres d'emploi
- Conseils CV, lettre de motivation
- Préparation aux entretiens
- Orientation professionnelle, développement de carrière

RÈGLES STRICTES :
- Tu ne parles JAMAIS d'études, d'examens scolaires (BAC, BFEM) ni de concours.
- Si l'utilisateur aborde ces sujets, recentre avec bienveillance sur son projet professionnel.
- Réponds en français, format WhatsApp (gras avec *, listes avec -, emojis avec parcimonie).
- Sois concret, utile et encourageant. Réponses courtes et claires."""


async def answer_emploi(text: str, user, db=None, history: list = None) -> str | None:
    """
    Génère une réponse emploi en texte libre.
    Utilisé en mode emploi uniquement pour tout message qui n'a pas été
    capté par les actions structurées (offres, profil, plan...).
    """
    try:
        user_context = await build_user_context(user, db)

        history_str = ""
        if history:
            recent = history[-6:] if len(history) > 6 else history
            lines = []
            for msg in recent:
                role = "Utilisateur" if msg.get("role") == "user" else "Prepa"
                lines.append(f"{role}: {msg.get('content', '')[:200]}")
            history_str = "\nHISTORIQUE RÉCENT :\n" + "\n".join(lines)

        prompt = f"""{user_context}
{history_str}

MESSAGE DE L'UTILISATEUR : "{text}"

Réponds de manière utile et bienveillante, uniquement sur l'emploi et la carrière."""

        return await _call_llm_text(EMPLOI_ASSISTANT_PROMPT, prompt)
    except Exception as e:
        print(f"  [bot_intelligence] answer_emploi error: {e}")
        return None


# ─── Helper : doit-on passer par le LLM ? ────────────────────────────────────

def should_analyze(text: str, user) -> bool:
    """
    Retourne True si le LLM doit analyser ce message.
    Skip si :
    - Texte trop court
    - ID bouton structuré (snake_case)
    - Commande / déjà gérée
    - État actif géré par le webhook (awaiting_copy, etc.)
    """
    if not text or len(text.strip()) < 2:
        return False

    t = text.strip()

    # IDs boutons WhatsApp (snake_case structuré)
    if re.match(r'^[a-z][a-z0-9_]{2,}$', t) and '_' in t:
        return False

    # Commandes explicites
    if t.startswith('/'):
        return False

    # États actifs → le webhook les gère directement
    conv = getattr(user, 'conversation_state', None) or {}
    if conv.get("awaiting_copy"):
        return False

    return True
