from app.services.llm.router import LLMRouter, LLMRequest, LLMResponse
from app.services.llm.providers import PROVIDERS
from app.services.llm.cache import semantic_cache
from app.services.config_service import config_service
import uuid

llm_router = LLMRouter()

SYSTEM_PROMPT = """
Tu es Prepa, professeur particulier expert du programme scolaire sénégalais (BFEM, BAC et concours).

MISSION PRINCIPALE :
Aider l'élève à comprendre, réussir ses exercices et préparer ses examens.

PRIORITÉ ABSOLUE AUX DOCUMENTS :

Lorsque des documents sont fournis :

1. Recherche d'abord la réponse dans les documents.
2. Base ta réponse principalement sur ces documents.
3. Utilise les mêmes définitions, notations, méthodes et formules que les documents.
4. Si plusieurs documents sont fournis, synthétise les informations pertinentes.
5. Si une information n'est pas présente dans les documents, complète avec tes connaissances générales.
6. En cas de conflit entre tes connaissances et les documents, privilégie les documents.

MÉTHODE DE RAISONNEMENT :

Avant de répondre :

1. Identifie les informations pertinentes dans le contexte.
2. Vérifie qu'elles répondent à la question.
3. Construis une explication logique et progressive.
4. Vérifie que la réponse est cohérente avec le contexte fourni.

PÉDAGOGIE :

- Explique clairement et progressivement.
- Adapte automatiquement le niveau de difficulté à l'élève.
- Mets l'accent sur la compréhension plutôt que la mémorisation.
- Utilise des exemples concrets lorsque c'est utile.
- Décompose les calculs et démonstrations en étapes.

QUESTIONS DE COURS :

Structure recommandée :

Définition
Propriétés importantes
Exemple
Point à retenir

EXERCICES :

Structure recommandée :

Méthode
Résolution étape par étape
Réponse finale
Conseil d'examen

CORRECTIONS :

- Analyse d'abord la démarche.
- Localise précisément l'erreur.
- Explique pourquoi elle est incorrecte.
- Montre la méthode correcte.
- Vérifie le résultat final.

FORMAT :

- Utilise WhatsApp.
- Titres courts en gras.
- Paragraphes aérés.
- Listes simples avec "-".
- Évite les réponses inutilement longues.

HORS SUJET :

Si la demande n'a aucun rapport avec les études ou la préparation d'examens :

"Je suis ton assistant de révision 📚. Pose-moi une question liée à tes cours ou à tes examens."

BALISES AUTORISÉES :

[FORMULE: expression]
[GRAPHE: expression, titre=Titre]
[TABLEAU: headers=A|B|C; rows=a1|b1|c1]
[SCHEMA: central=Concept; branches=A:detail|B:detail]
[CHRONO: annee=evenement]

PROFIL ÉLÈVE :

Nom : {student_name}
Examen : {exam_type}
Série : {series}
Niveau : {level}
Engagement : {engagement_score}/100
Streak : {streak_days} jours

CONTEXTE DOCUMENTAIRE :

{rag_context}

QUESTION :

{question}

INSTRUCTION :

Adapte automatiquement ton niveau d'explication au profil de l'élève.
Utilise prioritairement les documents fournis.
Si les documents ne suffisent pas, complète avec tes connaissances.
"""

def build_system_prompt(user) -> str:
    """Construit le prompt personnalisé selon le profil élève."""
    score = getattr(user, 'engagement_score', 50) or 50
    if score < 30:
        level = "1 (débutant)"
    elif score < 70:
        level = "2 (intermédiaire)"
    else:
        level = "3 (avancé)"

    return SYSTEM_PROMPT.format(
        student_name=getattr(user, 'name', 'élève') or 'élève',
        exam_type=getattr(user, 'exam_type', 'BAC') or 'BAC',
        series=getattr(user, 'series', 'S2') or 'S2',
        subjects=", ".join(getattr(user, 'subjects', ['maths']) or ['maths']),
        level=level,
        engagement_score=score,
        streak_days=getattr(user, 'streak_days', 0) or 0,
        rag_context="",   # sera rempli dans build_messages
        question="",      # sera rempli dans build_messages
    )


def build_messages(
    user_message: str,
    exam_type: str = "",
    subject: str = "",
    series: str = "",
    history: list = None,
    rag_context: str = "",
    chapitre: str = "",
    detection: dict = None,
    memory_context: str = "",
    exercise_context: str = "",
    user=None,
) -> list:
    """Construit la liste de messages pour le LLM avec mémoire structurée."""

    context = ""
    if exam_type:
        context += f"Examen : {exam_type.replace('_', ' ').title()}. "
    if series:
        context += f"Série : {series}. "
    if subject:
        context += f"Matière : {subject}. "
    if chapitre:
        context += f"Chapitre : {chapitre.replace('_', ' ').title()}. "

    # Contexte de détection
    detection_context = ""
    if detection:
        type_demande = detection.get("type_demande")
        niveau = detection.get("niveau_question", "intermediaire")
        mots_cles = detection.get("mots_cles", [])

        if type_demande == "exercice":
            detection_context += "\nL'élève demande un exercice d'entraînement — fournis un exercice complet avec corrigé."
        elif type_demande == "correction":
            detection_context += "\nL'élève soumet une réponse à corriger — analyse sa réponse et corrige avec bienveillance."
        elif type_demande == "methode":
            detection_context += "\nL'élève veut comprendre la méthode — explique étape par étape."

        if niveau == "debutant":
            detection_context += "\nNiveau débutant détecté — utilise des analogies simples et des exemples concrets."
        elif niveau == "avance":
            detection_context += "\nNiveau avancé détecté — tu peux aller plus loin dans les détails."

        if mots_cles:
            detection_context += f"\nConcepts clés identifiés : {', '.join(mots_cles[:5])}."

    system = build_system_prompt(user) if user else SYSTEM_PROMPT
    if context:
        system += f"\n\nContexte élève : {context}"
    if memory_context:
        system += f"\n\nMémoire session :\n{memory_context}"
    if exercise_context:
        system += f"\n\nTypes d'exercices connus sur ce chapitre :\n{exercise_context}"
    if detection_context:
        system += f"\n\nAnalyse de la demande :{detection_context}"
    if rag_context:
        system += f"\n\nExtrait du programme officiel sénégalais :\n{rag_context}\n\nBase ta réponse sur ces extraits officiels."

    msgs = [{"role": "system", "content": system}]

    # Historique court — max 4 échanges
    if history:
        msgs.extend(history[-4:])

    msgs.append({"role": "user", "content": user_message})
    return msgs


async def call_llm(
    user_message: str,
    user_plan: str = "free",
    exam_type: str = "",
    subject: str = "",
    series: str = "",
    complexity: int = 1,
    history: list = None,
    db=None,
    chapitre: str = "",
    detection: dict = None,
    user=None,
) -> LLMResponse:
    """Point d'entrée principal — RAG + profil cognitif + mémoire structurée."""

    # 1. Choisit le provider
    request = LLMRequest(
        user_plan=user_plan,
        message=user_message,
        exam_type=exam_type,
        subject=subject,
        complexity=complexity,
    )
    provider_name = await llm_router.route(request)
    max_tokens = await config_service.get_int("llm_max_tokens")

    # 2. Vérifie le cache
    cache_key = semantic_cache.make_key(user_message, provider_name)
    cached = await semantic_cache.get(cache_key)
    if cached:
        return LLMResponse(text=cached, provider=provider_name, from_cache=True)

    # 3. Récupère le contexte RAG granulaire
    rag_context = ""
    if db and (exam_type or series or subject or chapitre):
        try:
            from app.services.rag.search_service import search_service
            rag_context = await search_service.build_context(
                db=db,
                query=user_message,
                exam_type=exam_type or None,
                series=series or None,
                subject=subject or None,
                chapitre=chapitre or None,
                top_k=5,
            )
            if rag_context:
                print(f"RAG: contexte trouve ({len(rag_context)} chars) — {subject}/{chapitre}")
        except Exception as e:
            print(f"RAG error: {e}")

    # 4. Mémoire structurée — résumé session + profil cognitif
    memory_context = ""
    structured_history = history or []

    if db and detection:
        try:
            user_id_str = detection.get("user_id")
            if user_id_str:
                from app.repositories.message_repository import message_repo
                from app.services.rag.mastery_service import mastery_service

                # Profil cognitif
                mastery_str = ""
                if subject and chapitre:
                    mastery_str = await mastery_service.get_chapter_context(
                        db=db,
                        user_id=uuid.UUID(user_id_str),
                        matiere=subject,
                        chapitre=chapitre,
                    )
                    if mastery_str:
                        print(f"Mastery: {mastery_str}")

                # Historique court + résumé session
                if user:
                    structured_history, memory_context = await message_repo.get_structured_context(
                        db=db,
                        user_id=uuid.UUID(user_id_str),
                        user=user,
                        mastery_context=mastery_str,
                    )
                elif mastery_str:
                    memory_context = f"Profil élève : {mastery_str}"

        except Exception as e:
            print(f"Memory context error: {e}")
            structured_history = history or []

    # 5. Contexte types d'exercices depuis le cerveau
    exercise_context = ""
    if db and subject and chapitre:
        try:
            from app.services.rag.brain_service import brain_service
            exercise_context = await brain_service.get_exercise_context(
                db=db,
                matiere=subject,
                chapitre=chapitre,
                query=user_message,
            )
            if exercise_context:
                print(f"Exercise context: {len(exercise_context)} chars")
        except Exception as e:
            print(f"Exercise context error: {e}")

    # 6. Construit les messages avec mémoire structurée
    msgs = build_messages(
        user_message, exam_type, subject, series,
        structured_history, rag_context, chapitre,
        detection, memory_context, exercise_context,
        user=user,
    )

    # 7. Appelle le provider avec fallback
    providers_to_try = _get_fallback_chain(provider_name)

    for pname in providers_to_try:
        provider = PROVIDERS.get(pname)
        if not provider:
            continue
        try:
            text = await provider.complete(msgs, max_tokens)
            await semantic_cache.set(cache_key, text)
            return LLMResponse(text=text, provider=pname)
        except Exception as e:
            print(f"Provider {pname} failed: {e}")
            continue

    return LLMResponse(
        text="Je rencontre des difficultés techniques. Réessaie dans quelques instants. 🙏",
        provider="fallback",
    )


def _get_fallback_chain(primary: str) -> list[str]:
    """Retourne la chaîne de fallback selon le provider principal."""
    chains = {
        "groq":      ["groq", "mistral", "openai"],
        "mistral":   ["mistral", "groq", "openai"],
        "openai":    ["openai", "mistral", "groq"],
        "anthropic": ["anthropic", "openai", "mistral"],
    }
    return chains.get(primary, ["mistral", "groq", "openai"])