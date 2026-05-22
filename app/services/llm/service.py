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
- Pour les maths/physique, montre les étapes de résolution
- Utilise des exemples concrets du contexte africain
- Ne réponds qu'aux questions liées aux cours et révisions
- Si hors sujet, redirige poliment vers les révisions"""


def build_messages(
    user_message: str,
    exam_type: str = "",
    subject: str = "",
    series: str = "",
    history: list = None,
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

    messages = [{"role": "system", "content": system}]

    # Historique (max 5 derniers échanges)
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
) -> LLMResponse:
    """Point d'entrée principal pour appeler l'IA."""

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
        return LLMResponse(
            text=cached,
            provider=provider_name,
            from_cache=True,
        )

    # 3. Construit les messages
    msgs = build_messages(user_message, exam_type, subject, series, history)

    # 4. Appelle le provider avec fallback
    providers_to_try = _get_fallback_chain(provider_name)

    for pname in providers_to_try:
        provider = PROVIDERS.get(pname)
        if not provider:
            continue
        try:
            text = await provider.complete(msgs, max_tokens)
            # Met en cache
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