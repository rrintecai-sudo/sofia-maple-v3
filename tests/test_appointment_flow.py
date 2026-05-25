"""Tests del handler de agendado (Bloque C.1 PASO 5).

Cubre la lógica de `handle_appointment_intent`:
- Extracción de fecha falla → hint pide aclaración
- Fecha extraída pero NO disponible → hint con alternativas
- Fecha disponible pero falta nombre del papá → hint pide nombre
- Flujo feliz: crea lead + cita + emit_event + email stub
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx
from app.config import Settings
from app.core.appointment_extractor import (
    TZ_MONTERREY,
    AppointmentDateTime,
)
from app.core.appointment_flow import (
    AppointmentHandlerResult,
    handle_appointment_intent,
)
from app.core.state import (
    Canal,
    EstadoCapturado,
    EstadoConversacion,
    HijoInfo,
    NivelEducativo,
)


def _settings(lily_email: str = "") -> Settings:
    return Settings(
        env="test",
        supabase_url="https://x.supabase.co",
        supabase_service_key="srv-key",
        openai_api_key="sk-test",
        lily_email=lily_email,
    )


def _estado_base(
    *, nombre_papa: str | None = None, nivel: NivelEducativo | None = None
) -> EstadoConversacion:
    capt = EstadoCapturado(
        nombre_papa=nombre_papa,
        nivel_buscado_actual=nivel,
        hijos=[HijoInfo(nombre="Luis", edad=5, nivel=nivel)] if nivel else [],
    )
    return EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=capt,
    )


def _mock_extractor(
    monkeypatch, fecha: str | None, hora: str | None, confidence: float = 0.9
) -> None:
    """Reemplaza extract_datetime con un stub que devuelve los valores dados."""

    async def fake(mensaje: str, *, now=None):  # type: ignore[no-redef]
        return AppointmentDateTime(
            fecha=fecha, hora=hora, confidence=confidence, razonamiento="stub"
        )

    monkeypatch.setattr("app.core.appointment_flow.extract_datetime", fake)


# ============================================================
# Caso 1 — extractor no encuentra fecha → hint pide aclaración
# ============================================================


@pytest.mark.asyncio
async def test_handler_sin_fecha_pide_aclaracion(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha=None, hora=None, confidence=0.2)
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    result = await handle_appointment_intent("quiero agendar", estado, settings=_settings())
    assert isinstance(result, AppointmentHandlerResult)
    assert "extract_failed" in result.acciones
    assert "NO especificó fecha" in result.hint_para_prompt
    assert result.appointment_id is None


# ============================================================
# Caso 2 — fecha con baja confianza → hint pide aclaración
# ============================================================


@pytest.mark.asyncio
async def test_handler_confidence_baja_pide_aclaracion(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00", confidence=0.5)
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    result = await handle_appointment_intent("tal vez el martes", estado, settings=_settings())
    assert "extract_failed" in result.acciones


# ============================================================
# Caso 3 — fecha disponible pero falta nombre del papá
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_disponible_pero_sin_nombre_papa(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": 2,  # martes
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://x.supabase.co/rest/v1/leads").mock(return_value=httpx.Response(200, json=[]))

    estado = _estado_base(nombre_papa=None, nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am", estado, settings=_settings(), now=now
    )
    assert "missing_parent_name" in result.acciones
    assert "cómo te llamas" in result.hint_para_prompt
    assert result.appointment_id is None


# ============================================================
# Caso 4 — slot ocupado → hint con alternativas
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_slot_ocupado_propone_alternativas(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    # Hay cita a las 10:00 del martes — el papá pidió justo esa
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "fecha_hora": "2026-05-26T10:00:00-06:00",
                    "duracion_min": 60,
                    "status": "confirmada",
                }
            ],
        )
    )

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am", estado, settings=_settings(), now=now
    )
    assert "availability:slot_ocupado" in result.acciones
    assert "ya está ocupado" in result.hint_para_prompt
    assert result.appointment_id is None


# ============================================================
# Caso 5 — día no laborable
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_dia_no_laborable(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-30", hora="10:00")  # sábado
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el sábado a las 10", estado, settings=_settings(), now=now
    )
    assert "availability:dia_no_laborable" in result.acciones


# ============================================================
# Caso 6 — flujo feliz E2E (creates lead, appointment, event, email)
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_flujo_feliz_e2e(monkeypatch, caplog) -> None:
    """Papá conocido, fecha válida y libre → crea lead+cita, emit_event,
    advance_stage, send_email (stub)."""
    import logging as _logging

    caplog.set_level(_logging.WARNING)

    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")

    # lily_availability: martes 9-17
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": 2,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
            ],
        )
    )
    # No hay citas existentes
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )
    # Lead no existe en GET inicial, luego se crea
    leads_get_calls = {"count": 0}

    def leads_get_mock(request):
        leads_get_calls["count"] += 1
        if leads_get_calls["count"] == 1:
            return httpx.Response(200, json=[])
        # En el segundo GET (post-create) devolvemos el lead recién creado
        return httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "parent_name": "Ana",
                    "parent_phone": None,
                    "parent_email": None,
                    "child_name": "Luis",
                    "child_age": 5,
                    "nivel": "kinder",
                    "channel": "telegram",
                    "classification": None,
                    "stage": "contacto_inicial",
                    "source": "sofia_ai",
                    "conversation_session_id": "telegram:111",
                    "notes": None,
                }
            ],
        )

    respx.get("https://x.supabase.co/rest/v1/leads").mock(side_effect=leads_get_mock)
    respx.post("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(201, json=[{"id": 42}])
    )
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    # Crear cita
    respx.post("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(201, json=[{"id": 99}])
    )
    # Emit events
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am",
        estado,
        settings=_settings(lily_email="lily@maple.mx"),
        now=now,
    )

    assert result.lead_id == 42
    assert result.appointment_id == 99
    assert "appointment_created" in result.acciones
    assert "event_emitted" in result.acciones
    assert "stage_advanced" in result.acciones
    assert "email_sent_to_lily" in result.acciones
    assert "PENDIENTE de aprobación" in result.hint_para_prompt
    # NO debe afirmar que ya está confirmada
    assert "confirmada" not in result.hint_para_prompt.lower() or (
        "NO digas" in result.hint_para_prompt
    )


# ============================================================
# Caso 7 — sin lily_email → email se loggea sin destinatario
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_sin_lily_email_skip_destinatario(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": 2,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://x.supabase.co/rest/v1/leads").mock(
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
                    "nivel": None,
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
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    respx.post("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(201, json=[{"id": 99}])
    )
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am",
        estado,
        settings=_settings(lily_email=""),  # vacío
        now=now,
    )
    assert "email_skipped_no_recipient" in result.acciones
    assert result.appointment_id == 99


# ============================================================
# Caso 8 — fecha en el pasado → fecha_pasada
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_handler_fecha_pasada(monkeypatch) -> None:
    _mock_extractor(monkeypatch, fecha="2026-05-10", hora="10:00")
    # No mockeamos lily_availability porque availability_checker corta antes
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("ayer", estado, settings=_settings(), now=now)
    assert "availability:fecha_pasada" in result.acciones
    assert "ya pasó" in result.hint_para_prompt
