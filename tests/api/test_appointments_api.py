"""Tests de los endpoints /api/appointments/{id}/approve y /reject (Bloque C.1 PASO 7)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from app.api.appointments import router as appointments_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
def client(monkeypatch):
    """Inyecta admin_api_key vacía (modo dev = sin auth)."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "srv-key")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    get_settings.cache_clear()

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(appointments_router)
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def client_con_admin_key(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "srv-key")
    monkeypatch.setenv("ADMIN_API_KEY", "secret-key-xyz")
    get_settings.cache_clear()

    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(appointments_router)
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _mock_get_appointment_endpoint(
    appointment_id: int, status: str = "pendiente", campus_id: int | None = 1
):
    """Mock del endpoint GET /rest/v1/appointments?id=eq.{id}"""
    return respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": appointment_id,
                    "lead_id": 42,
                    "fecha_hora": "2026-05-26T16:00:00+00:00",
                    "duracion_min": 60,
                    "status": status,
                    "notas": None,
                    "campus_id": campus_id,
                }
            ],
        )
    )


def _mock_get_campus_endpoint(campus_id: int = 1):
    """Mock GET /rest/v1/campus?id=eq.<id>"""
    if campus_id == 1:
        row = {
            "id": 1,
            "nombre": "Campus 1",
            "direccion": "José Figueroa Siller 156",
            "colonia": "Doctores",
            "ciudad": "Saltillo",
            "estado": "Coahuila",
            "pais": "México",
            "niveles": ["maternal", "kinder_1", "kinder_2", "kinder_3"],
            "notas": None,
            "vigente": True,
            "google_maps_url": "https://www.google.com/maps/search/?api=1&query=Jos%C3%A9+Figueroa+Siller+156",
        }
    else:
        row = {
            "id": 2,
            "nombre": "Campus 2",
            "direccion": "Blvd. V. Carranza 5064",
            "colonia": "Doctores",
            "ciudad": "Saltillo",
            "estado": "Coahuila",
            "pais": "México",
            "niveles": ["primaria_6", "secundaria_1", "secundaria_2", "secundaria_3"],
            "notas": None,
            "vigente": True,
            "google_maps_url": "https://www.google.com/maps/search/?api=1&query=Blvd.+V.+Carranza+5064",
        }
    return respx.get("https://x.supabase.co/rest/v1/campus").mock(
        return_value=httpx.Response(200, json=[row])
    )


def _mock_get_lead_session():
    """Mock para el GET /rest/v1/leads?id=eq.42 (sólo select session_id) y
    posterior get_lead_by_session."""
    return respx.get("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "parent_name": "Ana",
                    "parent_phone": None,
                    "parent_email": None,
                    "child_name": None,
                    "child_age": None,
                    "nivel": "kinder",
                    "channel": "telegram",
                    "classification": None,
                    "stage": "filtro_completado",
                    "source": "sofia_ai",
                    "conversation_session_id": "telegram:111",
                    "notes": None,
                }
            ],
        )
    )


# ============================================================
# Auth
# ============================================================


def test_approve_requiere_admin_key_si_configurada(client_con_admin_key: TestClient) -> None:
    resp = client_con_admin_key.post("/api/appointments/55/approve", json={})
    assert resp.status_code == 403


def test_approve_acepta_admin_key_valida(client_con_admin_key: TestClient) -> None:
    """Con la key correcta, llega al handler (404 porque no hay mock — eso es esperado)."""
    with respx.mock:
        respx.get("https://x.supabase.co/rest/v1/appointments").mock(
            return_value=httpx.Response(200, json=[])
        )
        resp = client_con_admin_key.post(
            "/api/appointments/55/approve",
            json={},
            headers={"X-Admin-Key": "secret-key-xyz"},
        )
    assert resp.status_code == 404  # cita no existe — la auth pasó


def test_reject_requiere_admin_key_si_configurada(client_con_admin_key: TestClient) -> None:
    resp = client_con_admin_key.post("/api/appointments/55/reject", json={})
    assert resp.status_code == 403


# ============================================================
# Approve
# ============================================================


@respx.mock
def test_approve_appointment_ok(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "pendiente", campus_id=1)
    _mock_get_lead_session()
    _mock_get_campus_endpoint(1)
    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    with patch(
        "app.adapters.dispatcher.send_message_to_session",
        new_callable=AsyncMock,
        return_value={"sent": True, "channel": "telegram", "detail": None},
    ):
        resp = client.post("/api/appointments/55/approve", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "confirmada"
    assert data["session_id"] == "telegram:111"


@respx.mock
def test_approve_cita_no_existe_devuelve_404(client: TestClient) -> None:
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )
    resp = client.post("/api/appointments/999/approve", json={})
    assert resp.status_code == 404


@respx.mock
def test_approve_cita_ya_confirmada_devuelve_409(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "confirmada")
    resp = client.post("/api/appointments/55/approve", json={})
    assert resp.status_code == 409
    assert "confirmada" in resp.json()["detail"]


@respx.mock
def test_approve_con_approved_by_lo_pasa_a_metadata(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "pendiente", campus_id=1)
    _mock_get_lead_session()
    _mock_get_campus_endpoint(1)
    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )

    captured_event = {}

    def capture_event(request):
        import json as _json

        captured_event.update(_json.loads(request.content))
        return httpx.Response(201, json=[{"id": 1}])

    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(side_effect=capture_event)

    with patch(
        "app.adapters.dispatcher.send_message_to_session",
        new_callable=AsyncMock,
        return_value={"sent": True, "channel": "telegram"},
    ):
        resp = client.post("/api/appointments/55/approve", json={"approved_by": "lily@maple.mx"})
    assert resp.status_code == 200
    assert captured_event["metadata"]["approved_by"] == "lily@maple.mx"


# ============================================================
# Reject (cancelar)
# ============================================================


@respx.mock
def test_reject_cancelar_sin_alternativa(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "pendiente")
    _mock_get_lead_session()
    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    with patch(
        "app.adapters.dispatcher.send_message_to_session",
        new_callable=AsyncMock,
        return_value={"sent": True, "channel": "telegram"},
    ):
        resp = client.post(
            "/api/appointments/55/reject",
            json={"reason": "horario de Lily cambió"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "cancelada"
    assert data["status"] == "cancelada"


@respx.mock
def test_reject_con_alternativa_reagenda(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "pendiente")
    _mock_get_lead_session()
    captured_patch = {}

    def capture(request):
        import json as _json

        captured_patch.update(_json.loads(request.content))
        return httpx.Response(204, text="")

    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(side_effect=capture)
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    with patch(
        "app.adapters.dispatcher.send_message_to_session",
        new_callable=AsyncMock,
        return_value={"sent": True, "channel": "telegram"},
    ):
        resp = client.post(
            "/api/appointments/55/reject",
            json={"alternative_datetime": "2026-05-27T11:00:00-06:00"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "reagendada"
    assert "2026-05-27" in data["fecha_hora"]
    # El PATCH a appointments incluyó fecha_hora nueva (NO status, sigue pendiente)
    assert "2026-05-27" in captured_patch["fecha_hora"]
    assert "status" not in captured_patch


@respx.mock
def test_reject_alternative_datetime_invalido(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "pendiente")
    _mock_get_lead_session()
    resp = client.post(
        "/api/appointments/55/reject",
        json={"alternative_datetime": "no-es-iso"},
    )
    assert resp.status_code == 400
    assert "ISO" in resp.json()["detail"]


@respx.mock
def test_reject_cita_ya_cancelada_devuelve_409(client: TestClient) -> None:
    _mock_get_appointment_endpoint(55, "cancelada")
    resp = client.post("/api/appointments/55/reject", json={"reason": "x"})
    assert resp.status_code == 409


# ============================================================
# Dispatcher integration (mensaje al papá)
# ============================================================


@respx.mock
def test_approve_dispatcher_recibe_session_id_y_texto(client: TestClient) -> None:
    """Verifica que el dispatcher recibe el session_id correcto + el mensaje
    de confirmación que contiene fecha humanizada, dirección del campus y
    link de Google Maps (Bloque C.2 PASO 4)."""
    _mock_get_appointment_endpoint(55, "pendiente", campus_id=1)
    _mock_get_lead_session()
    _mock_get_campus_endpoint(1)
    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, text: str):
        sends.append((session_id, text))
        return {"sent": True, "channel": "telegram", "detail": None}

    with patch("app.api.appointments.send_message_to_session", side_effect=fake_send):
        resp = client.post("/api/appointments/55/approve", json={})

    assert resp.status_code == 200
    assert len(sends) == 1
    sid, texto = sends[0]
    assert sid == "telegram:111"
    # Mensaje incluye nombre del papá (de _mock_get_lead_session → "Ana")
    assert "Ana" in texto
    # D.4 (Gaby 27-may): template oficial con "confirmó"
    assert "confirmó" in texto.lower() or "confirmamos" in texto.lower()
    # Estructura visual oficial: 📅 / 🕐 / 📍 / 🗺️
    assert "📅" in texto and "📍" in texto and "🗺️" in texto
    # Bloque C.2: dirección + link Maps incluidos
    assert "Campus 1" in texto
    assert "José Figueroa Siller 156" in texto
    assert "Col. Doctores" in texto
    assert "https://www.google.com/maps" in texto
