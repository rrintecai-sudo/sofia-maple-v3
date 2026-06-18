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
    *,
    nombre_papa: str | None = None,
    nivel: NivelEducativo | None = None,
    email_papa: str | None = "ana@example.com",
    telefono: str | None = "8441234567",
    grado_hijo: str | None = "1° kinder",
    nombre_hijo: str = "Luis",
    edad_hijo: int = 5,
) -> EstadoConversacion:
    """Fixture base. D.3 (Lily 27-may): por default trae los 6 datos del lead
    completos para que el flujo feliz pase. Tests que validan campos faltantes
    sobreescriben el campo correspondiente a None."""
    capt = EstadoCapturado(
        nombre_papa=nombre_papa,
        telefono=telefono,
        email_papa=email_papa,
        nivel_buscado_actual=nivel,
        hijos=(
            [HijoInfo(nombre=nombre_hijo, edad=edad_hijo, nivel=nivel, grado=grado_hijo)]
            if nivel
            else []
        ),
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


def _mock_campus_endpoint(campus_id: int):
    """Mock GET /rest/v1/campus?id=eq.<id> con la fila completa que usa el handler."""
    if campus_id == 1:
        row = {
            "id": 1,
            "nombre": "Campus 1",
            "direccion": "José Figueroa Siller 156",
            "colonia": "Doctores",
            "ciudad": "Saltillo",
            "estado": "Coahuila",
            "pais": "México",
            "niveles": [
                "maternal",
                "kinder_1",
                "kinder_2",
                "kinder_3",
                "primaria_1",
                "primaria_2",
                "primaria_3",
                "primaria_4",
                "primaria_5",
            ],
            "notas": None,
            "vigente": True,
            "google_maps_url": (
                "https://www.google.com/maps/search/?api=1"
                "&query=Jos%C3%A9+Figueroa+Siller+156%2C+Col.+Doctores%2C+Saltillo"
            ),
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
            "niveles": [
                "primaria_6",
                "secundaria_1",
                "secundaria_2",
                "secundaria_3",
            ],
            "notas": None,
            "vigente": True,
            "google_maps_url": (
                "https://www.google.com/maps/search/?api=1"
                "&query=Blvd.+V.+Carranza+5064%2C+Col.+Doctores%2C+Saltillo"
            ),
        }
    return respx.get("https://x.supabase.co/rest/v1/campus").mock(
        return_value=httpx.Response(200, json=[row])
    )


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
    # El código pide UN solo campo: el día (Haiku solo lo frasea).
    assert "día" in result.hint_para_prompt.lower()
    assert result.appointment_id is None


# ============================================================
# Caso 2 — fecha con baja confianza → hint pide aclaración
# ============================================================


@pytest.mark.asyncio
async def test_handler_confidence_baja_pide_aclaracion(monkeypatch) -> None:
    # Mensaje SIN día de semana ni fecha explícita → el resolver determinístico no
    # aplica y, con baja confianza del LLM, la fecha queda sin resolver → pide día.
    # (Un "el martes" sí se resolvería determinísticamente; ver test dedicado.)
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00", confidence=0.5)
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    result = await handle_appointment_intent(
        "tal vez cuando se pueda", estado, settings=_settings()
    )
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
    # D.3 (Lily 27-may): falta solo el nombre del papá → ahora cae en
    # missing_lead_data con esa entrada específica
    assert any(a.startswith("missing_lead_data") for a in result.acciones)
    assert "tu nombre" in result.hint_para_prompt
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
    # C.2 PASO 5: el handler resuelve campus (Kinder → Campus 1) y consulta
    # la fila completa para incluir dirección + Maps en el hint.
    _mock_campus_endpoint(1)

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
    # C.2: campus resuelto + dirección + Maps incluidos en el hint
    assert result.campus_id == 1
    assert "Campus 1" in result.hint_para_prompt
    assert "José Figueroa Siller 156" in result.hint_para_prompt
    assert "https://www.google.com/maps" in result.hint_para_prompt
    # NO debe afirmar que ya está confirmada
    assert "confirmada" not in result.hint_para_prompt.lower() or (
        "NO digas" in result.hint_para_prompt
    )


# ============================================================
# CRÍTICO — el correo NUNCA es load-bearing: si Resend/red falla, la cita igual
# se crea y el cierre (appointment_id + campus) igual se devuelve.
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_correo_falla_cita_se_crea_igual(monkeypatch) -> None:
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
    respx.get("https://x.supabase.co/rest/v1/leads").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(201, json=[{"id": 42}])
    )
    respx.post("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(201, json=[{"id": 99}])
    )
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 1}])
    )
    _mock_campus_endpoint(1)

    # El correo al papá REVIENTA (simula caída total de send_email).
    async def boom(*a, **k):
        raise RuntimeError("resend caído / red muerta")

    monkeypatch.setattr("app.core.appointment_flow.send_email", boom)

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am", estado, settings=_settings(lily_email="lily@maple.mx"), now=now
    )

    # La cita SE CREÓ y el cierre se devuelve (appointment_id + campus) → D.4 dispara.
    assert result.appointment_id == 99
    assert result.campus_id == 1
    assert "appointment_created" in result.acciones
    assert "Campus 1" in result.hint_para_prompt


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
    _mock_campus_endpoint(1)

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
    # La hora del LLM ya NO se toma fuera del paso de la hora → la fecha pasada se
    # rechaza en el paso del DÍA (1b), no en el de disponibilidad. Sigue siendo fecha_pasada.
    assert any("fecha_pasada" in a for a in result.acciones)
    assert "ya pasó" in result.hint_para_prompt or "ya no es posible" in result.hint_para_prompt


@pytest.mark.asyncio
@respx.mock
async def test_handler_hoy_pasado_cierre_explica_motivo(monkeypatch) -> None:
    """Pulido 1: 'hoy' a las 3 p.m. se mueve a jueves 11 y Sofía EXPLICA por qué
    (no salta de día en silencio)."""
    _mock_extractor(monkeypatch, fecha=None, hora=None)  # el LLM no aporta fecha
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "08:00:00",
                    "end_time": "15:00:00",
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
    now = datetime(2026, 6, 10, 15, 0, tzinfo=TZ_MONTERREY)  # miércoles 3 p.m.
    result = await handle_appointment_intent("hoy", estado, settings=_settings(), now=now)

    assert estado.estado_capturado.cita_fecha_slot == "2026-06-11"  # se movió a jueves
    msg = (result.mensaje_coleccion or "").lower()
    assert "cerramos" in msg  # explica la razón
    assert "jueves 11 de junio" in msg  # nombra el día propuesto
    assert "hora" in msg  # y pide la hora de ese día


def _mock_lily_lv_8a15() -> None:
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "08:00:00",
                    "end_time": "15:00:00",
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


@pytest.mark.asyncio
@respx.mock
async def test_handler_paso_dia_propone_fechas_concretas(monkeypatch) -> None:
    """Nuevo enfoque: el paso del día PROPONE 2-3 fechas concretas (no abre a parseo)."""
    _mock_extractor(monkeypatch, fecha=None, hora=None)
    _mock_lily_lv_8a15()
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 6, 10, 15, 0, tzinfo=TZ_MONTERREY)  # miércoles 3 p.m.
    result = await handle_appointment_intent(
        "quiero agendar", estado, settings=_settings(), now=now
    )

    msg = (result.mensaje_coleccion or "").lower()
    assert "tengo disponible" in msg
    assert "jueves 11" in msg and "viernes 12" in msg and "lunes 15" in msg
    assert estado.estado_capturado.opciones_dia_propuestas == [
        "2026-06-11",
        "2026-06-12",
        "2026-06-15",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_handler_papa_elige_opcion_dia(monkeypatch) -> None:
    """El papá elige una de las fechas ofrecidas → toma esa fecha y pasa a la hora."""
    _mock_extractor(monkeypatch, fecha=None, hora=None)
    _mock_lily_lv_8a15()
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    estado.estado_capturado.opciones_dia_propuestas = [
        "2026-06-11",
        "2026-06-12",
        "2026-06-15",
    ]
    estado.estado_capturado.ultimo_campo_pedido = "dia"
    now = datetime(2026, 6, 10, 15, 0, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("el jueves", estado, settings=_settings(), now=now)

    assert estado.estado_capturado.cita_fecha_slot == "2026-06-11"
    assert estado.estado_capturado.opciones_dia_propuestas == []  # ya eligió
    assert "hora" in (result.mensaje_coleccion or "").lower()  # pasó a la hora


@pytest.mark.asyncio
@respx.mock
async def test_handler_hoy_en_paso_dia_no_se_vuelve_hora(monkeypatch) -> None:
    """BUG: 'hoy' en el paso del día NO debe volverse una hora alucinada por el LLM
    ('esa hora fuera de horario'). Resuelve la FECHA y pasa a pedir la hora."""
    _mock_extractor(monkeypatch, fecha="2026-06-10", hora="15:30")  # LLM alucina hora
    _mock_lily_lv_8a15()
    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    estado.estado_capturado.ultimo_campo_pedido = "dia"  # estamos en el paso del DÍA
    now = datetime(2026, 6, 10, 15, 0, tzinfo=TZ_MONTERREY)  # miércoles 3 p.m.
    result = await handle_appointment_intent("hoy", estado, settings=_settings(), now=now)

    assert estado.estado_capturado.cita_hora_slot is None  # la hora del LLM NO se tomó
    texto = (result.hint_para_prompt + (result.mensaje_coleccion or "")).lower()
    assert "fuera" not in texto  # NO "esa hora fuera de horario"
    assert "hora" in (result.mensaje_coleccion or "").lower()  # ahora sí pide la hora


# ============================================================
# Bloque C.2 — Campus resuelto automáticamente desde el nivel del hijo
# (NUNCA preguntado al papá) + mensaje incluye dirección + link Maps
# ============================================================


def _setup_happy_flow_mocks(*, lead_nivel: str, lead_child_age: int | None = None) -> None:
    """Mocks comunes para los tests de campus: availability libre, appointments
    vacíos, lead que avanza stage, campus fetch."""
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
    respx.get("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "parent_name": "Ana",
                    "parent_phone": None,
                    "parent_email": None,
                    "child_name": "Luis",
                    "child_age": lead_child_age,
                    "nivel": lead_nivel,
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


@pytest.mark.asyncio
@respx.mock
async def test_campus_kinder_es_campus_1(monkeypatch) -> None:
    """Papá pide cita para Kinder → cita queda con campus_id=1."""
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    _setup_happy_flow_mocks(lead_nivel="kinder", lead_child_age=5)
    _mock_campus_endpoint(1)

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("martes 10am", estado, settings=_settings(), now=now)
    assert result.campus_id == 1
    assert "Campus 1" in result.hint_para_prompt


@pytest.mark.asyncio
@respx.mock
async def test_campus_secundaria_es_campus_2(monkeypatch) -> None:
    """Papá pide cita para Secundaria → cita queda con campus_id=2."""
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    _setup_happy_flow_mocks(lead_nivel="secundaria", lead_child_age=13)
    _mock_campus_endpoint(2)

    estado = EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=EstadoCapturado(
            nombre_papa="Ana",
            telefono="8441234567",
            email_papa="ana@example.com",
            nivel_buscado_actual=NivelEducativo.SECUNDARIA,
            hijos=[
                HijoInfo(
                    nombre="Luis",
                    edad=13,
                    nivel=NivelEducativo.SECUNDARIA,
                    grado="1° secundaria",
                )
            ],
        ),
    )
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("martes 10am", estado, settings=_settings(), now=now)
    assert result.campus_id == 2
    assert "Campus 2" in result.hint_para_prompt
    assert "Blvd. V. Carranza 5064" in result.hint_para_prompt


@pytest.mark.asyncio
@respx.mock
async def test_campus_primaria_quinto_es_campus_1(monkeypatch) -> None:
    """Primaria 5° → Campus 1 (edad 10)."""
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    _setup_happy_flow_mocks(lead_nivel="primaria", lead_child_age=10)
    _mock_campus_endpoint(1)

    estado = EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=EstadoCapturado(
            nombre_papa="Ana",
            telefono="8441234567",
            email_papa="ana@example.com",
            nivel_buscado_actual=NivelEducativo.PRIMARIA,
            hijos=[
                HijoInfo(
                    nombre="Luis",
                    edad=10,
                    nivel=NivelEducativo.PRIMARIA,
                    grado="5to primaria",
                )
            ],
        ),
    )
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("martes 10am", estado, settings=_settings(), now=now)
    assert result.campus_id == 1


@pytest.mark.asyncio
@respx.mock
async def test_campus_primaria_sexto_es_campus_2(monkeypatch) -> None:
    """Primaria 6° → Campus 2 (caso crítico: cambia de campus). El grado se
    captura desde grado_texto del hijo."""
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    _setup_happy_flow_mocks(lead_nivel="primaria", lead_child_age=11)
    _mock_campus_endpoint(2)

    estado = EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=EstadoCapturado(
            nombre_papa="Ana",
            telefono="8441234567",
            email_papa="ana@example.com",
            nivel_buscado_actual=NivelEducativo.PRIMARIA,
            hijos=[
                HijoInfo(
                    nombre="Luis",
                    edad=11,
                    nivel=NivelEducativo.PRIMARIA,
                    grado="6to primaria",
                )
            ],
        ),
    )
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("martes 10am", estado, settings=_settings(), now=now)
    assert result.campus_id == 2
    assert "Campus 2" in result.hint_para_prompt
    # Bloque C.2: dirección y Maps incluidos en el hint
    assert "Blvd. V. Carranza 5064" in result.hint_para_prompt
    assert "https://www.google.com/maps" in result.hint_para_prompt


@pytest.mark.asyncio
@respx.mock
async def test_campus_primaria_sin_grado_pide_grado(monkeypatch) -> None:
    """Primaria sin edad ni grado → handler retorna 'missing_grado', NO crea
    la cita ni resuelve campus."""
    _mock_extractor(monkeypatch, fecha="2026-05-26", hora="10:00")
    # Mocks de availability + lead (necesarios porque el handler corre todo
    # hasta llegar al resolve campus)
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
                    "nivel": "primaria",
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

    # D.3 (Lily 27-may): los demás datos del lead están completos; solo
    # falta el grado del hijo. Eso ya se captura como missing_lead_data
    # antes de llegar al campus_resolver.
    estado = EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=EstadoCapturado(
            nombre_papa="Ana",
            telefono="8441234567",
            email_papa="ana@example.com",
            nivel_buscado_actual=NivelEducativo.PRIMARIA,
            hijos=[
                HijoInfo(
                    nombre="Luis",
                    edad=None,  # FIX 1: sin edad NI grado no se puede deducir → se pide
                    nivel=NivelEducativo.PRIMARIA,
                )
            ],
        ),
    )
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent("martes 10am", estado, settings=_settings(), now=now)
    assert any(a.startswith("missing_lead_data") for a in result.acciones)
    assert "grado escolar del hijo" in result.acciones[0]
    assert result.appointment_id is None
    # Sin edad NI grado, el código pide la EDAD primero (el grado se DEDUCE de
    # ella); Haiku no elige preguntar el grado.
    assert "edad" in result.hint_para_prompt.lower()


# ============================================================
# D.3 (Lily 2026-05-27): datos_lead_faltantes
# ============================================================


def test_datos_faltantes_lista_vacia_con_lead_completo() -> None:
    from app.core.appointment_flow import datos_lead_faltantes

    estado = _estado_base(nombre_papa="Ana", nivel=NivelEducativo.KINDER)
    assert datos_lead_faltantes(estado) == []


def test_datos_faltantes_detecta_email_celular_y_grado() -> None:
    """FIX 1: el grado solo se pide si NO hay edad (sin edad no se deduce)."""
    from app.core.appointment_flow import datos_lead_faltantes

    estado = _estado_base(
        nombre_papa="Ana",
        nivel=NivelEducativo.PRIMARIA,
        email_papa=None,
        telefono=None,
        grado_hijo=None,
        edad_hijo=None,  # sin edad → el grado no se puede deducir → se pide
    )
    faltan = datos_lead_faltantes(estado)
    assert "correo electrónico" in faltan
    assert "número de celular" in faltan
    assert "grado escolar del hijo" in faltan


def test_datos_faltantes_no_pide_grado_si_hay_edad() -> None:
    """FIX 1: con edad conocida, el grado se DEDUCE (no se pide)."""
    from app.core.appointment_flow import datos_lead_faltantes

    estado = _estado_base(
        nombre_papa="Ana",
        nivel=NivelEducativo.PRIMARIA,
        grado_hijo=None,
        edad_hijo=8,
    )
    faltan = datos_lead_faltantes(estado)
    assert "grado escolar del hijo" not in faltan
    assert faltan == []  # con edad+demás datos, nada falta


# ============================================================
# FIX (2026-06-02) — fecha/hora ancladas al AHORA + disponibilidad real
# ============================================================


def _mock_disponibilidad_lun_vie_8_15() -> None:
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "08:00:00",
                    "end_time": "15:00:00",
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


@pytest.mark.asyncio
@respx.mock
async def test_handler_hoy_lunes_9pm_no_ofrece_hoy(monkeypatch) -> None:
    """'el lunes' a las 21:00 del lunes → HOY ya cerró. El resolver determinístico
    (FIX 2026-06-02) NO ofrece hoy: resuelve 'el lunes' al PRÓXIMO lunes (la
    semana siguiente) y pide la hora para ese día. Mejor que el viejo
    'descarta y propón martes': respeta el día que pidió el papá."""
    _mock_disponibilidad_lun_vie_8_15()
    _mock_extractor(monkeypatch, fecha="2026-06-08", hora=None, confidence=0.9)  # hoy lunes

    estado = EstadoConversacion(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=EstadoCapturado(),
    )
    now = datetime(2026, 6, 8, 21, 0, tzinfo=TZ_MONTERREY)  # lunes 9pm
    result = await handle_appointment_intent("el lunes", estado, settings=_settings(), now=now)

    # Resolvió al PRÓXIMO lunes (2026-06-15), NUNCA hoy (2026-06-08).
    assert estado.estado_capturado.cita_fecha_slot == "2026-06-15"
    assert result.appointment_id is None
    assert "missing_time" in result.acciones  # ahora pide la hora de ESE lunes


# ============================================================
# FIX (2026-06-02) — al REUSAR lead, actualizar edad/nivel con lo nuevo
# ============================================================


@pytest.mark.asyncio
async def test_ensure_lead_actualiza_edad_y_nivel_al_reusar(monkeypatch) -> None:
    """Lead existente edad 3/maternal + conversación nueva '4 años'/kinder →
    el lead se ACTUALIZA a 4 / Kinder 2° (no se queda con lo viejo)."""
    import types

    from app.core.appointment_flow import _ensure_lead_para_cita

    existing = types.SimpleNamespace(
        id=17,
        child_name="Emanuel",
        child_age=3,
        child_grade=None,
        nivel="maternal",
        parent_name="Oscar Rodriguez",
        parent_phone="+17866035862",
        parent_email="ing2oscar@gmail.com",
    )
    captured: dict = {}

    async def fake_get_lead(session_id, *, settings=None):
        return existing

    async def fake_update(lead_id, updates, *, settings=None):
        captured["_id"] = lead_id
        captured.update(updates)

    monkeypatch.setattr("app.core.appointment_flow.get_lead_by_session", fake_get_lead)
    monkeypatch.setattr("app.core.appointment_flow.update_lead", fake_update)

    estado = EstadoConversacion(
        session_id="web:x",
        canal=Canal.WEB,
        identificador="x",
        estado_capturado=EstadoCapturado(
            nombre_papa="Oscar Rodriguez",
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
            hijos=[
                HijoInfo(
                    nombre="Emanuel", edad=4, nivel=NivelEducativo.KINDER, grado="2° de Kinder"
                )
            ],
        ),
    )
    lead_id = await _ensure_lead_para_cita(estado, settings=_settings())
    assert lead_id == 17
    assert captured.get("_id") == 17
    assert captured.get("child_age") == 4  # 3 → 4
    assert captured.get("nivel") == "kinder"  # maternal → kinder
    assert captured.get("child_grade") == "2° de Kinder"


@pytest.mark.asyncio
async def test_ensure_lead_no_actualiza_si_no_cambia(monkeypatch) -> None:
    """Si los datos nuevos coinciden con el lead, no hay update innecesario."""
    import types

    from app.core.appointment_flow import _ensure_lead_para_cita

    existing = types.SimpleNamespace(
        id=17,
        child_name="Emanuel",
        child_age=4,
        child_grade="2° de Kinder",
        nivel="kinder",
        parent_name="Oscar Rodriguez",
        parent_phone="+17866035862",
        parent_email="ing2oscar@gmail.com",
    )
    llamado = {"update": False}

    async def fake_get_lead(session_id, *, settings=None):
        return existing

    async def fake_update(lead_id, updates, *, settings=None):
        llamado["update"] = True

    monkeypatch.setattr("app.core.appointment_flow.get_lead_by_session", fake_get_lead)
    monkeypatch.setattr("app.core.appointment_flow.update_lead", fake_update)

    estado = EstadoConversacion(
        session_id="web:x",
        canal=Canal.WEB,
        identificador="x",
        estado_capturado=EstadoCapturado(
            nombre_papa="Oscar Rodriguez",
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
            hijos=[
                HijoInfo(
                    nombre="Emanuel", edad=4, nivel=NivelEducativo.KINDER, grado="2° de Kinder"
                )
            ],
        ),
    )
    await _ensure_lead_para_cita(estado, settings=_settings())
    assert llamado["update"] is False


# ============================================================
# FIX 1 (2026-06-01) — derivar nivel/grado de la edad
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edad,pref,cat,grado",
    [
        (3, None, "maternal", None),  # 3 default → Maternal (Toddlers)
        (3, "kinder", "kinder", "1° de Kinder"),  # papá dice kinder → K1
        (4, "kinder", "kinder", "2° de Kinder"),  # K2 = 4
        (4, None, "kinder", "2° de Kinder"),  # 4 → Kinder 2 (no maternal a esa edad)
        (5, None, "kinder", "3° de Kinder"),  # K3 = 5
        (6, None, "primaria", "1° de Primaria"),  # 72m
        (1, None, "maternal", None),  # 12m → Babies (maternal, sin grado)
        # Primaria 4-6 + Secundaria (confirmado Lily 2026, numeración RELATIVA):
        (9, "primaria", "primaria", "4° de Primaria"),
        (10, "primaria", "primaria", "5° de Primaria"),
        (11, "primaria", "primaria", "6° de Primaria"),
        (12, "secundaria", "secundaria", "1° de Secundaria"),
        (13, "secundaria", "secundaria", "2° de Secundaria"),  # caso real Emma/Emanuel
        (13, None, "secundaria", "2° de Secundaria"),  # sin preferencia, igual
        (14, "secundaria", "secundaria", "3° de Secundaria"),
    ],
)
async def test_derivar_nivel_grado_de_edad(edad, pref, cat, grado) -> None:
    """Sin Supabase usa el espejo determinístico _NIVELES_FALLBACK."""
    from app.tools.niveles import derivar_nivel_grado_de_edad

    res = await derivar_nivel_grado_de_edad(edad, nivel_preferido=pref)
    assert res is not None
    categoria, g, _display = res
    assert categoria == cat
    assert g == grado


@pytest.mark.asyncio
async def test_derivar_edad_fuera_de_tabla_devuelve_none() -> None:
    from app.tools.niveles import derivar_nivel_grado_de_edad

    assert await derivar_nivel_grado_de_edad(20) is None
    assert await derivar_nivel_grado_de_edad(None) is None


# ============================================================
# POLÍTICA A (2026-06-04) — el grado DECLARADO por el papá manda sobre la edad
# ============================================================


@pytest.mark.asyncio
async def test_politica_a_grado_declarado_manda_sobre_edad() -> None:
    """Caso María: declaró '1° de Primaria' (canónico) + edad 7 (derivaría 2°) →
    se RESPETA el declarado, NO se sobreescribe."""
    from app.core.appointment_flow import _consolidar_y_derivar_hijo
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        hijos=[
            HijoInfo(nombre="Juan", edad=7, nivel=NivelEducativo.PRIMARIA, grado="1° de Primaria")
        ]
    )
    derivado = await _consolidar_y_derivar_hijo(capt)
    assert capt.hijos[0].grado == "1° de Primaria"  # declarado, NO 2°
    assert derivado is None  # no derivó (había grado canónico)


@pytest.mark.asyncio
async def test_politica_a_sin_grado_declarado_deriva_por_edad() -> None:
    """Sin grado declarado, la edad SIGUE derivando (no rompe lo de ayer)."""
    from app.core.appointment_flow import _consolidar_y_derivar_hijo
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        hijos=[HijoInfo(nombre="Ema", edad=4, nivel=NivelEducativo.KINDER, grado=None)]
    )
    await _consolidar_y_derivar_hijo(capt)
    assert capt.hijos[0].grado == "2° de Kinder"  # derivado por edad


@pytest.mark.asyncio
async def test_politica_a_grado_parcial_aun_se_deriva() -> None:
    """Un grado PARCIAL ('kinder' sin año) NO es canónico → la edad lo completa
    (FIX 2 sigue vigente bajo Política A)."""
    from app.core.appointment_flow import _consolidar_y_derivar_hijo
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        hijos=[HijoInfo(nombre="Lu", edad=5, nivel=NivelEducativo.KINDER, grado="kinder")]
    )
    await _consolidar_y_derivar_hijo(capt)
    assert capt.hijos[0].grado == "3° de Kinder"


def test_datos_faltantes_maternal_no_pide_grado() -> None:
    """En Maternal, la edad determina el sub-grupo (Cubs/Baby/Infants/Toddlers).
    No se exige grado_hijo separado."""
    from app.core.appointment_flow import datos_lead_faltantes

    estado = _estado_base(
        nombre_papa="Ana",
        nivel=NivelEducativo.MATERNAL,
        grado_hijo=None,  # explícitamente sin grado
        edad_hijo=2,
    )
    faltan = datos_lead_faltantes(estado)
    assert "grado escolar del hijo" not in faltan


def test_datos_faltantes_detecta_nombre_hijo_y_edad_faltantes() -> None:
    from app.core.appointment_flow import datos_lead_faltantes
    from app.core.state import EstadoCapturado as _EK
    from app.core.state import EstadoConversacion as _EC

    estado = _EC(
        session_id="telegram:111",
        canal=Canal.TELEGRAM,
        identificador="111",
        estado_capturado=_EK(
            nombre_papa="Ana",
            telefono="8441234567",
            email_papa="ana@example.com",
            nivel_buscado_actual=NivelEducativo.KINDER,
            # sin hijos
        ),
    )
    faltan = datos_lead_faltantes(estado)
    assert "nombre del hijo" in faltan
    assert "edad del hijo" in faltan


@pytest.mark.asyncio
@respx.mock
async def test_handler_missing_lead_data_corta_antes_de_crear_cita(monkeypatch) -> None:
    """Si faltan datos del lead, NO se crea la cita aunque el slot esté libre."""
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

    estado = _estado_base(
        nombre_papa="Ana",
        nivel=NivelEducativo.KINDER,
        email_papa=None,  # falta
        telefono=None,  # falta
    )
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await handle_appointment_intent(
        "el martes 10am", estado, settings=_settings(), now=now
    )
    assert any(a.startswith("missing_lead_data") for a in result.acciones)
    assert result.appointment_id is None
    # El código pide UN solo dato (el correo, primero faltante por prioridad);
    # NO pide el celular en el mismo turno y enumera lo ya capturado (Ana).
    assert "correo" in result.hint_para_prompt.lower()
    assert "ana" in result.hint_para_prompt.lower()


# ============================================================
# D.3 — prompt agendado.md documenta los 6 datos requeridos
# ============================================================


def test_agendado_prompt_documenta_6_datos_requeridos() -> None:
    from app.core.prompt_builder import clear_cache, load_prompt_file

    clear_cache()
    md = load_prompt_file("journey/agendado.md").lower()
    assert "6 datos" in md or "seis datos" in md
    assert "nombre del alumno" in md
    assert "grado escolar" in md
    assert "correo electrónico" in md
    assert "número de celular" in md
    clear_cache()
