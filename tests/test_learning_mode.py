"""Tests del módulo learning_mode + endpoint admin."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from app.api.admin import router as admin_router
from app.config import Settings
from app.core.learning_mode import (
    CATEGORIAS_FEEDBACK,
    guardar_feedback,
    listar_feedback_pendiente,
    revisar_feedback,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _settings() -> Settings:
    return Settings(
        supabase_url="https://x.supabase.co",
        supabase_service_key="sk-svc",
        admin_api_key="admin-secret",
    )


def test_categorias_son_inmutables() -> None:
    assert "tono" in CATEGORIAS_FEEDBACK
    assert "precio" in CATEGORIAS_FEEDBACK
    assert "otro" in CATEGORIAS_FEEDBACK


@pytest.mark.asyncio
@respx.mock
async def test_guardar_feedback_inserta_y_devuelve_id() -> None:
    respx.post("https://x.supabase.co/rest/v1/sofia_feedback_pending").mock(
        return_value=httpx.Response(201, json=[{"id": 42}])
    )
    fid = await guardar_feedback(
        session_id="telegram:1",
        feedback_text="Sofía no debe decir 'platícame' tres veces",
        contexto_anterior="hola\nrespuesta previa",
        categoria="tono",
        settings=_settings(),
    )
    assert fid == 42


@pytest.mark.asyncio
async def test_guardar_feedback_sin_supabase_returna_none() -> None:
    fid = await guardar_feedback(
        session_id="web:1",
        feedback_text="x",
        settings=Settings(),
    )
    assert fid is None


@pytest.mark.asyncio
@respx.mock
async def test_guardar_feedback_no_levanta_excepcion_si_falla() -> None:
    """Resilient: si Supabase cae, no debe explotar el orchestrator."""
    respx.post("https://x.supabase.co/rest/v1/sofia_feedback_pending").mock(
        side_effect=httpx.ConnectError("boom")
    )
    fid = await guardar_feedback("telegram:1", "x", settings=_settings())
    assert fid is None


@pytest.mark.asyncio
@respx.mock
async def test_listar_feedback_pendiente() -> None:
    respx.get("https://x.supabase.co/rest/v1/sofia_feedback_pending").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "session_id": "telegram:1",
                    "feedback_text": "ajustar tono",
                    "contexto_anterior": None,
                    "propuesta_cambio": None,
                    "categoria": "tono",
                    "estado": "pending",
                    "created_at": "2026-05-19T00:00:00Z",
                    "revised_by": None,
                    "revised_at": None,
                    "pr_url": None,
                    "notas_revision": None,
                }
            ],
        )
    )
    items = await listar_feedback_pendiente(settings=_settings())
    assert len(items) == 1
    assert items[0].id == 1
    assert items[0].categoria == "tono"


@pytest.mark.asyncio
@respx.mock
async def test_revisar_feedback_actualiza() -> None:
    respx.patch("https://x.supabase.co/rest/v1/sofia_feedback_pending").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "estado": "approved"}])
    )
    ok = await revisar_feedback(
        feedback_id=5,
        decision="approved",
        revised_by="oscar",
        notas="creo PR mañana",
        settings=_settings(),
    )
    assert ok is True


@pytest.mark.asyncio
@respx.mock
async def test_revisar_feedback_falla() -> None:
    respx.patch("https://x.supabase.co/rest/v1/sofia_feedback_pending").mock(
        return_value=httpx.Response(500, text="server error")
    )
    ok = await revisar_feedback(
        feedback_id=99,
        decision="rejected",
        settings=_settings(),
    )
    assert ok is False


# ============================================================
# Admin endpoints
# ============================================================


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
def admin_client(monkeypatch):
    """Cliente FastAPI con el router de admin montado y admin_api_key configurada."""
    from app.config import get_settings as gs

    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    gs.cache_clear()

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(admin_router)
    with TestClient(app) as c:
        yield c

    gs.cache_clear()


def test_admin_requires_key(admin_client: TestClient) -> None:
    """Sin X-Admin-Key correcta → 403."""
    resp = admin_client.get("/admin/feedback/pending")
    assert resp.status_code == 403


def test_admin_with_correct_key(monkeypatch, admin_client: TestClient) -> None:
    """Con X-Admin-Key válida → 200 (con mock de Supabase)."""
    # Mock listar_feedback_pendiente para que no haga llamada real
    from app.api import admin as admin_module

    async def fake_list(**kwargs):
        return []

    monkeypatch.setattr(admin_module, "listar_feedback_pendiente", fake_list)

    resp = admin_client.get(
        "/admin/feedback/pending",
        headers={"X-Admin-Key": "admin-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_admin_review_endpoint(monkeypatch, admin_client: TestClient) -> None:
    from app.api import admin as admin_module

    async def fake_revisar(**kwargs):
        return True

    monkeypatch.setattr(admin_module, "revisar_feedback", fake_revisar)

    resp = admin_client.post(
        "/admin/feedback/42/review",
        headers={"X-Admin-Key": "admin-secret"},
        json={"decision": "approved", "revised_by": "oscar"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 42, "decision": "approved"}
