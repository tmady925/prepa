from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_name: str = "Prepa"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_base_url: str = "http://localhost:8000"

    admin_secret_key: str = "prepa_admin_2026"

    # Database
    database_url: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_config_ttl: int = 60
    redis_cache_ttl: int = 604800

    # WhatsApp — Wasender
    wasender_api_key: str = ""
    wasender_base_url: str = "https://api.wasender.com/api"
    whatsapp_number: str = "221789939028"
    whatsapp_webhook_secret: str = ""

    # LLM
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    mistral_api_key: str = ""
    groq_api_key: str = ""
    zyte_api_key: str = ""

    # PayDunya
    paydunya_master_key: str = ""
    paydunya_private_key: str = ""
    paydunya_public_key: str = ""
    paydunya_token: str = ""
    paydunya_mode: str = "test"

    # Cloudinary
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""

    # LLM routing
    llm_default_free_provider: str = "mistral"
    llm_default_pro_provider: str = "openai"
    llm_max_tokens_default: int = 300

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()