"""Tests de los endpoints /healthz y /readyz con FastAPI TestClient."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from app import __version__
from app.api.health import router as health_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
def client():
    """App de test sin lifespan real — no abre pools de Postgres/Redis."""
    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(health_router)
    with TestClient(app) as c:
        yield c


def test_healthz_returns_200(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == __version__
    assert "ts" in data


def test_readyz_all_skip_when_no_config(client: TestClient) -> None:
    """Con todas las API keys vacías y servicios inalcanzables, devuelve degraded o not_ready."""
    # Mockear los health_checks para que devuelvan skip
    with (
        patch(
            "app.api.health.get_supabase",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
        patch(
            "app.api.health.get_postgres",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
        patch(
            "app.api.health.get_redis",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
        patch(
            "app.api.health.get_anthropic",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
        patch(
            "app.api.health.get_openai",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    # Todos en skip → todavía ready (skip no es unreachable)
    assert data["status"] == "ready"
    assert set(data["dependencies"].keys()) == {
        "supabase",
        "postgres",
        "redis",
        "anthropic",
        "openai",
    }


def test_readyz_returns_503_when_critical_unreachable(client: TestClient) -> None:
    """Si una dependencia crítica (postgres) está unreachable, devuelve 503."""
    with (
        patch(
            "app.api.health.get_supabase",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
        patch(
            "app.api.health.get_postgres",
            return_value=type(
                "X",
                (),
                {
                    "health_check": AsyncMock(
                        return_value={"status": "unreachable", "detail": "boom"}
                    )
                },
            )(),
        ),
        patch(
            "app.api.health.get_redis",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
        patch(
            "app.api.health.get_anthropic",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
        patch(
            "app.api.health.get_openai",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "skip"})}
            )(),
        ),
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


def test_readyz_degraded_when_optional_unreachable(client: TestClient) -> None:
    """Si una opcional (anthropic) está unreachable pero las críticas ok → degraded."""
    with (
        patch(
            "app.api.health.get_supabase",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
        patch(
            "app.api.health.get_postgres",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
        patch(
            "app.api.health.get_redis",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
        patch(
            "app.api.health.get_anthropic",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "unreachable"})}
            )(),
        ),
        patch(
            "app.api.health.get_openai",
            return_value=type(
                "X", (), {"health_check": AsyncMock(return_value={"status": "ok"})}
            )(),
        ),
    ):
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
