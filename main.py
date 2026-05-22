from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.settings import get_settings
from app.db.redis import connect_redis, disconnect_redis

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_redis()
    yield
    await disconnect_redis()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    debug=settings.app_debug,
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {"app": settings.app_name, "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


from app.api.v1.endpoints import webhook, payments
app.include_router(webhook.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1")