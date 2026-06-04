import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.core.settings import get_settings
from app.db.redis import connect_redis, disconnect_redis

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_redis()

    # Lance le scheduler simulations en arrière-plan
    from app.services.simulation_scheduler import run_scheduler
    scheduler_task = asyncio.create_task(run_scheduler())

    # Lance le scheduler de scraping emploi (toutes les 6h)
    from app.services.scraping_scheduler import run_scraping_scheduler
    scraping_task = asyncio.create_task(run_scraping_scheduler())

    yield

    # Arrêt propre des schedulers
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

    scraping_task.cancel()
    try:
        await scraping_task
    except asyncio.CancelledError:
        pass

    await disconnect_redis()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    debug=settings.app_debug,
    lifespan=lifespan,
)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"app": settings.app_name, "status": "running"}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}


from app.api.v1.endpoints import webhook, payments, admin, tasks, recruiters
app.include_router(webhook.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(recruiters.router, prefix="/api/v1")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        timeout_keep_alive=300,   # 5 min — pour les uploads longs (exercices)
        h11_max_incomplete_event_size=16 * 1024 * 1024,  # 16 MB
    )