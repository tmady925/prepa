from dataclasses import dataclass
from app.services.config_service import config_service


@dataclass
class LLMRequest:
    user_plan: str          # free | pro | famille
    message: str            # message de l'élève
    exam_type: str = ""     # bac_senegal | bfem | concours
    subject: str = ""       # maths | physique | français…
    complexity: int = 1     # 1=simple 2=moyen 3=complexe


@dataclass
class LLMResponse:
    text: str
    provider: str
    tokens_used: int = 0
    from_cache: bool = False


class LLMRouter:

    async def route(self, request: LLMRequest) -> str:
        """Retourne le nom du provider à utiliser."""

        # Pro → provider payant
        if request.user_plan == "pro":
            if request.subject in ("maths", "physique") and request.complexity >= 3:
                return "anthropic"
            return await config_service.get("llm_default_pro_provider") or "openai"

        # Free → provider gratuit selon complexité
        if request.complexity >= 3:
            return "mistral"

        return await config_service.get("llm_default_free_provider") or "groq"

    async def get_max_tokens(self) -> int:
        return await config_service.get_int("llm_max_tokens")


llm_router = LLMRouter()