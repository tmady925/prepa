from app.services.llm.router import LLMRouter, LLMRequest, LLMResponse
from app.services.llm.providers import PROVIDERS
from app.services.llm.cache import semantic_cache
from app.services.config_service import config_service

llm_router = LLMRouter()

SYSTEM_PROMPT = """Tu es Prepa, un assistant pédagogique intelligent qui aide les élèves africains francophones à réviser pour leurs examens.

Règles strictes :
- Réponds toujours en français simple et clair
- Adapte ton niveau à celui de l'élève
- Sois encourageant et bienveillant
- Réponds de façon concise (max 300 mots)
- Utilise des exemples concrets du contexte africain (Dakar, Thiès, etc.)
- Ne réponds qu'aux questions liées aux cours et révisions
- Si hors sujet, redirige poliment vers les révisions
- Utilise uniquement *texte* pour le gras et - pour les listes

Pour les formules mathématiques, utilise ce format exact :
[FORMULE: f(x) = x^2 + 2x + 1]

Pour les graphes de fonctions :
[GRAPHE: x^2 - 2*x + 1, titre=Graphe de f(x)]

Pour les tableaux de données :
[TABLEAU: headers=Col1|Col2|Col3; rows=val1|val2|val3; val4|val5|val6; titre=Mon tableau]

Pour les chronologies :
[CHRONO: 1960=Independance Senegal; 1962=Creation UPS; titre=Histoire du Senegal]

Pour les schémas conceptuels :
[SCHEMA: central=Photosynthese; branches=Definition:Transformation lumiere|Reactifs:CO2 et H2O|Produits:O2 et glucose]

N'utilise ces balises QUE quand c'est vraiment utile pour comprendre."""


def build_messages(
    user_message: str,
    exam_type: str = "",
    subject: str = "",
    series: str = "",
    history: list = None,
    rag_context: str = "",
) -> list:
    """Construit la liste de messages pour le LLM."""

    context = ""
    if exam_type:
        context += f"Examen : {exam_type.replace('_', ' ').title()}. "
    if series:
        context += f"Série : {series}. "
    if subject:
        context += f"Matière : {subject}. "

    system = SYSTEM_PROMPT
    if context:
        system += f"\n\nContexte élève : {context}"

    if rag_context:
        system += f"\n\nExtrait du programme officiel sénégalais :\n{rag_context}\n\nBase ta réponse sur ces extraits officiels."

    messages = [{"role": "system", "content": system}]

    if history:
        messages.extend(history[-10:])

    messages.append({"role": "user", "content": user_message})
    return messages


async def call_llm(
    user_message: str,
    user_plan: str = "free",
    exam_type: str = "",
    subject: str = "",
    series: str = "",
    complexity: int = 1,
    history: list = None,
    db=None,
) -> LLMResponse:
    """Point d'entrée principal pour appeler l'IA avec RAG."""

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

    # 3. Récupère le contexte RAG si db disponible
    rag_context = ""
    if db and (exam_type or series or subject):
        try:
            from app.services.rag.search_service import search_service
            rag_context = await search_service.build_context(
                db=db,
                query=user_message,
                exam_type=exam_type or None,
                series=series or None,
                subject=subject or None,
                top_k=3,
            )
            if rag_context:
                print(f"RAG: contexte trouve ({len(rag_context)} chars)")
        except Exception as e:
            print(f"RAG error: {e}")

    # 4. Construit les messages avec contexte RAG
    msgs = build_messages(user_message, exam_type, subject, series, history, rag_context)

    # 5. Appelle le provider avec fallback
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