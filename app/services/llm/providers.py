import httpx
from app.core.settings import get_settings

settings = get_settings()


class GroqProvider:
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    MODEL = "llama-3.3-70b-versatile"

    async def complete(self, messages: list, max_tokens: int = 300) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.BASE_URL,
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": self.MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                },
            )
            data = response.json()
            return data["choices"][0]["message"]["content"]


class MistralProvider:
    BASE_URL = "https://api.mistral.ai/v1/chat/completions"
    MODEL = "mistral-small-latest"

    async def complete(self, messages: list, max_tokens: int = 300) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.BASE_URL,
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": self.MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                },
            )
            data = response.json()
            return data["choices"][0]["message"]["content"]


class OpenAIProvider:
    BASE_URL = "https://api.openai.com/v1/chat/completions"
    MODEL = "gpt-4o-mini"

    async def complete(self, messages: list, max_tokens: int = 300) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.BASE_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": self.MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                },
            )
            data = response.json()
            return data["choices"][0]["message"]["content"]


class AnthropicProvider:
    BASE_URL = "https://api.anthropic.com/v1/messages"
    MODEL = "claude-haiku-4-5-20251001"

    async def complete(self, messages: list, max_tokens: int = 300) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.BASE_URL,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                },
            )
            data = response.json()
            return data["content"][0]["text"]


PROVIDERS = {
    "groq": GroqProvider(),
    "mistral": MistralProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
}