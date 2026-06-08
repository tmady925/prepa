import json
from typing import Any
from app.db.redis import get_redis
from app.core.settings import get_settings

settings = get_settings()

# Valeurs par défaut — utilisées si la clé n'existe pas en base
DEFAULTS = {
    # Quotas
    "free_messages_per_day": 10,
    "referral_bonus_messages": 20,
    "referral_paid_bonus": 50,
    "referral_job_bonus_active": 1,
    "referral_job_bonus_paid": 1,
    "referral_active_days": 7,
    "referral_min_msgs": 7,
    "bonus_expiry_days": 30,

    # Plans
    "plan_free_price_fcfa": 0,
    "plan_pro_price_fcfa": 500,
    "plan_famille_price_fcfa": 1200,
    "plan_famille_max_users": 4,

    # LLM
    "llm_max_tokens": 300,
    "llm_context_messages": 5,
    "llm_semantic_cache_threshold": 0.92,

    # RAG
    "rag_min_similarity": 0.4,
    # Upsell
    "upsell_min_streak_days": 5,
    "upsell_min_msgs_today": 8,
    "upsell_cooldown_days": 3,
    "upsell_engagement_threshold": 50,

    # Engagement
    "streak_reset_hours": 36,
    "daily_reset_hour": 0,

    # Mode
    "fascicule_mode": True,

    # Notifications
    "notification_blocker_enabled": True,  # Si False, toutes les notifs sont envoyées directement (queue ignorée)
}


class ConfigService:

    async def get(
        self,
        key: str,
        scope: str = "global",
        scope_id: str | None = None,
    ) -> Any:
        cache_key = f"config:{scope}:{scope_id or 'global'}:{key}"
        redis = await get_redis()

        # 1. Cherche dans Redis
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                return json.loads(cached)
        except Exception:
            pass

        # 2. Cherche dans PostgreSQL
        try:
            from app.db.database import AsyncSessionLocal
            from app.models.config import PlatformConfig
            from sqlalchemy import select, or_

            async with AsyncSessionLocal() as session:
                stmt = (
                    select(PlatformConfig)
                    .where(
                        PlatformConfig.key == key,
                        PlatformConfig.scope == scope,
                        or_(
                            PlatformConfig.scope_id == scope_id,
                            PlatformConfig.scope_id.is_(None),
                        ),
                    )
                    .order_by(PlatformConfig.scope_id.nulls_last())
                    .limit(1)
                )
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()

                if row is not None:
                    value = row.value
                    await redis.setex(cache_key, settings.redis_config_ttl, json.dumps(value))
                    return value
        except Exception:
            pass

        # 3. Valeur par défaut
        default = DEFAULTS.get(key)
        if default is not None:
            await redis.setex(cache_key, settings.redis_config_ttl, json.dumps(default))
        return default

    async def set(
        self,
        key: str,
        value: Any,
        scope: str = "global",
        scope_id: str | None = None,
        description: str | None = None,
        updated_by: str | None = None,
    ) -> None:
        from app.db.database import AsyncSessionLocal
        from app.models.config import PlatformConfig
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            stmt = select(PlatformConfig).where(
                PlatformConfig.key == key,
                PlatformConfig.scope == scope,
                PlatformConfig.scope_id == scope_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row:
                row.value = value
                if updated_by:
                    row.updated_by = updated_by
            else:
                row = PlatformConfig(
                    key=key,
                    value=value,
                    value_type=type(value).__name__,
                    scope=scope,
                    scope_id=scope_id,
                    description=description,
                    updated_by=updated_by,
                )
                session.add(row)

            await session.commit()

        # Invalide le cache Redis
        cache_key = f"config:{scope}:{scope_id or 'global'}:{key}"
        redis = await get_redis()
        await redis.delete(cache_key)

    async def get_int(self, key: str, scope: str = "global", scope_id: str | None = None) -> int:
        return int(await self.get(key, scope, scope_id) or 0)

    async def get_float(self, key: str, scope: str = "global", scope_id: str | None = None) -> float:
        return float(await self.get(key, scope, scope_id) or 0.0)

    async def get_bool(self, key: str, scope: str = "global", scope_id: str | None = None) -> bool:
        return bool(await self.get(key, scope, scope_id))


config_service = ConfigService()