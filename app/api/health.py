"""Endpoints de health y readiness.

- `/healthz`: liveness — la app está corriendo (200 siempre que el proceso responda).
- `/readyz`: readiness — Sofía puede llegar a sus dependencias (Supabase, Redis, OpenAI, Anthropic).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app import __version__
from app.adapters.anthropic_client import get_anthropic
from app.adapters.openai_client import get_openai
from app.adapters.postgres_client import get_postgres
from app.adapters.redis_client import get_redis
from app.adapters.supabase_client import get_supabase

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    ts: str


class DependencyStatus(BaseModel):
    status: Literal["ok", "unauthorized", "unreachable", "skip"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded", "not_ready"]
    version: str
    ts: str
    dependencies: dict[str, DependencyStatus]


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe. Devuelve 200 si la app puede responder."""
    return HealthResponse(
        version=__version__,
        ts=datetime.now(UTC).isoformat(),
    )


@router.get("/readyz", response_model=ReadinessResponse)
async def readyz(response: Response) -> ReadinessResponse:
    """Readiness probe.

    Estado:
    - **ready**: todas las dependencias críticas en `ok` o `skip`.
    - **degraded**: alguna dependencia opcional sin auth, app sigue funcional.
    - **not_ready**: alguna dependencia crítica unreachable → HTTP 503.

    Críticas: Supabase, Postgres, Redis.
    Opcionales (skip permitido en dev): Anthropic, OpenAI.
    """
    checks: dict[str, Any] = await asyncio.gather(
        get_supabase().health_check(),
        get_postgres().health_check(),
        get_redis().health_check(),
        get_anthropic().health_check(),
        get_openai().health_check(),
        return_exceptions=False,
    )
    deps_raw: dict[str, dict[str, Any]] = dict(
        zip(
            ["supabase", "postgres", "redis", "anthropic", "openai"],
            checks,
            strict=True,
        )
    )
    deps = {name: DependencyStatus(**payload) for name, payload in deps_raw.items()}

    criticas = ["supabase", "postgres", "redis"]
    overall: Literal["ready", "degraded", "not_ready"] = "ready"

    for name in criticas:
        if deps[name].status == "unreachable":
            overall = "not_ready"
            break

    if overall == "ready":
        # Si una opcional está unreachable → degraded
        for name in ("anthropic", "openai"):
            if deps[name].status == "unreachable":
                overall = "degraded"
                break

    if overall == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status=overall,
        version=__version__,
        ts=datetime.now(UTC).isoformat(),
        dependencies=deps,
    )
