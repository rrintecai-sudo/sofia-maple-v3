"""Entrypoint FastAPI.

Arranca: `uv run uvicorn app.main:app --reload`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.adapters.postgres_client import get_postgres
from app.adapters.redis_client import get_redis
from app.api.admin import router as admin_router
from app.api.appointments import router as appointments_router
from app.api.health import router as health_router
from app.api.webhook_telegram import router as webhook_telegram_router
from app.api.webhook_web import router as webhook_web_router
from app.api.webhook_whatsapp import router as webhook_whatsapp_router
from app.config import get_settings
from app.core.repository import get_repository
from app.observability.logger import get_logger, setup_logging

log = get_logger(__name__)

WEB_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifecycle: conectar pools al arranque, cerrarlos al shutdown."""
    settings = get_settings()
    setup_logging(level=settings.log_level)
    log.info(
        "starting sofia-maple",
        extra={"version": __version__, "env": settings.env},
    )

    # Conectar dependencias críticas — si fallan, levantamos warning pero no abortamos
    # (eso es trabajo de /readyz). El proceso debe vivir para reportar el problema.
    pg = get_postgres()
    if pg.is_configured():
        try:
            await pg.connect()
        except Exception as exc:
            log.warning("postgres connection failed at startup", extra={"error": str(exc)})

    redis = get_redis()
    try:
        await redis.connect()
    except Exception as exc:
        log.warning("redis connection failed at startup", extra={"error": str(exc)})

    yield

    # Shutdown
    log.info("shutting down sofia-maple")
    await pg.disconnect()
    await redis.disconnect()
    await get_repository().close()


def create_app() -> FastAPI:
    """Factory de la app — útil para testing."""
    app = FastAPI(
        title="Sofía 2.0 — Maple Collège",
        version=__version__,
        description="Embajadora digital de admisiones de Maple Collège.",
        lifespan=lifespan,
        # Sin docs en prod por defecto (las habilita admin si quiere)
        docs_url="/docs",
        redoc_url=None,
    )

    app.include_router(health_router)
    app.include_router(webhook_web_router)
    app.include_router(webhook_telegram_router)
    app.include_router(webhook_whatsapp_router)
    app.include_router(admin_router)
    app.include_router(appointments_router)

    # Static files para el Web Chat
    if WEB_STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_STATIC_DIR), name="static")

    return app


app = create_app()
