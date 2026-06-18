"""Tests de los helpers de notifications/integrations (Bloque C.1 PASO 6):
- app/notifications/email.py        — stub + render del email a Lily
- app/integrations/events.py        — emit_event a activity_events
- app/integrations/leads.py         — get/create/update + advance_stage
- app/integrations/appointments.py  — create/get/update appointments
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
import pytest
import respx
from app.config import Settings
from app.core.appointment_extractor import TZ_MONTERREY
from app.integrations.appointments import (
    Appointment,
    create_appointment,
    get_appointment,
    update_appointment,
)
from app.integrations.events import emit_event
from app.integrations.leads import (
    Lead,
    advance_stage_if_lower,
    create_lead,
    get_lead_by_session,
    update_lead,
)
from app.notifications.email import (
    EmailPayload,
    render_cita_pendiente_email,
    render_confirmacion_email_papa,
    send_email,
)


def _settings() -> Settings:
    return Settings(
        env="test",
        supabase_url="https://x.supabase.co",
        supabase_service_key="srv-key",
    )


# ============================================================
# email — send_email (stub) + render
# ============================================================


@pytest.mark.asyncio
async def test_send_email_stub_loggea(caplog) -> None:
    """Sin RESEND_API_KEY → cae al stub (solo log), delivered=False."""
    caplog.set_level(logging.WARNING)
    result = await send_email("lily@maple.mx", "Asunto X", "Body Y", settings=_settings())
    assert isinstance(result, EmailPayload)
    assert result.delivered is False
    assert result.provider == "stub"
    assert any("email_stub_send" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_email_destinatario_vacio(caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = await send_email("", "asunto", "body", settings=_settings())
    assert result.delivered is False
    assert any("email_skip_destinatario_vacio" in r.message for r in caplog.records)


def _settings_resend() -> Settings:
    return Settings(
        env="test",
        supabase_url="https://x.supabase.co",
        supabase_service_key="srv-key",
        resend_api_key="re_test_key",
        email_from="Maple Collège <notificaciones@maplecollege.rrintecai.co>",
    )


@pytest.mark.asyncio
async def test_send_email_resend_ok(monkeypatch) -> None:
    """Con RESEND_API_KEY, POSTea a Resend y marca delivered=True con el id."""
    import httpx

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "resend-abc-123"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    res = await send_email(
        "papa@correo.com", "Asunto", "Cuerpo del correo", settings=_settings_resend()
    )
    assert res.delivered is True
    assert res.provider == "resend"
    assert res.provider_id == "resend-abc-123"
    # Envió el From, To y text correctos a Resend.
    assert captured["url"].endswith("/emails")
    assert captured["json"]["from"] == "Maple Collège <notificaciones@maplecollege.rrintecai.co>"
    assert captured["json"]["to"] == ["papa@correo.com"]
    assert captured["json"]["text"] == "Cuerpo del correo"
    assert "re_test_key" in captured["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_send_email_resend_falla_no_lanza(monkeypatch, caplog) -> None:
    """Si Resend lanza (red caída), send_email NO propaga: delivered=False + error."""
    import httpx

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    caplog.set_level(logging.WARNING)
    res = await send_email("papa@correo.com", "S", "B", settings=_settings_resend())
    assert res.delivered is False
    assert res.error and "network down" in res.error
    assert any("email_resend_error" in r.message for r in caplog.records)


def test_render_confirmacion_email_papa() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.tools.campus import CampusResult

    campus = CampusResult(
        id=2,
        nombre="Campus 2",
        direccion="Blvd. V. Carranza 5064",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["secundaria_1"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Blvd",
    )
    dt = datetime(2026, 6, 4, 16, 0, tzinfo=ZoneInfo("America/Monterrey"))
    subject, text, html = render_confirmacion_email_papa(
        nombre_papa="Emma Rangel", fecha_hora=dt, campus=campus
    )
    assert subject == "Confirmación de tu cita de informes — Maple Collège"
    # Texto literal de Gaby + datos en ambas versiones
    for cuerpo in (text, html):
        assert "Emma Rangel" in cuerpo
        assert "Te confirmamos tu cita de informes para conocer Maple Collège" in cuerpo
        assert "jueves 4 de junio de 2026" in cuerpo  # mismo formato que D.4
        assert "4:00 p.m." in cuerpo
        assert "Campus 2" in cuerpo
        assert "Blvd. V. Carranza 5064" in cuerpo  # dirección de la TABLA campus
        assert "recorrido por las instalaciones" in cuerpo
    # El link de Maps: en TEXTO la URL cruda; en HTML el <a> clickeable.
    assert "https://www.google.com/maps/search/?api=1&query=Blvd" in text
    assert (
        '<a href="https://www.google.com/maps/search/?api=1&amp;query=Blvd">'
        "Ver ubicación en Google Maps</a>" in html
    )


def test_render_cita_pendiente_email_completo() -> None:
    subject, body = render_cita_pendiente_email(
        nombre_papa="Juan Pérez",
        nombre_hijo="Luis",
        edad_hijo=5,
        nivel="kinder",
        fecha_hora_iso="2026-05-26T10:00",
        canal="whatsapp",
        appointment_id=42,
        approval_url="https://maple.platform/appointments/42",
    )
    assert "Juan Pérez" in subject
    assert "2026-05-26T10:00" in subject
    assert "Juan Pérez" in body
    assert "Luis" in body
    assert "5 años" in body
    assert "kinder" in body
    assert "whatsapp" in body
    assert "42" in body
    assert "https://maple.platform/appointments/42" in body


def test_render_cita_pendiente_email_sin_hijo() -> None:
    """Si no hay nombre de hijo, el body no debe romper."""
    _subject, body = render_cita_pendiente_email(
        nombre_papa="Ana",
        nombre_hijo=None,
        edad_hijo=None,
        nivel=None,
        fecha_hora_iso="2026-05-26T10:00",
        canal="telegram",
        appointment_id=7,
    )
    assert "Ana" in body
    assert "telegram" in body
    assert "7" in body


def test_render_cita_pendiente_papa_nulo_usa_placeholder() -> None:
    subject, body = render_cita_pendiente_email(
        nombre_papa=None,
        nombre_hijo=None,
        edad_hijo=None,
        nivel=None,
        fecha_hora_iso="2026-06-01T10:00",
        canal="web",
        appointment_id=1,
    )
    assert "Papá/mamá" in subject or "papá" in body.lower()


# ============================================================
# emit_event
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_emit_event_ok() -> None:
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(201, json=[{"id": 99}])
    )
    eid = await emit_event(
        "sofia_appointment_scheduled",
        lead_id=12,
        session_id="telegram:123",
        description="cita pendiente",
        metadata={"fecha_hora": "2026-05-26T10:00"},
        settings=_settings(),
    )
    assert eid == 99


@pytest.mark.asyncio
@respx.mock
async def test_emit_event_metadata_incluye_session_id() -> None:
    """Si pasamos session_id, se mete dentro de metadata."""
    called_payload = {}

    def capture(request):
        import json as _json

        called_payload.update(_json.loads(request.content))
        return httpx.Response(201, json=[{"id": 1}])

    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(side_effect=capture)
    await emit_event(
        "lead_created",
        lead_id=5,
        session_id="whatsapp:5218441",
        settings=_settings(),
    )
    assert called_payload["metadata"]["session_id"] == "whatsapp:5218441"


@pytest.mark.asyncio
@respx.mock
async def test_emit_event_supabase_500_graceful() -> None:
    respx.post("https://x.supabase.co/rest/v1/activity_events").mock(
        return_value=httpx.Response(500, text="server error")
    )
    eid = await emit_event("sofia_classified", settings=_settings())
    assert eid is None  # no lanza


@pytest.mark.asyncio
async def test_emit_event_sin_supabase_url(caplog) -> None:
    caplog.set_level(logging.WARNING)
    settings = Settings(env="test", supabase_url="")
    eid = await emit_event("sofia_classified", settings=settings)
    assert eid is None
    assert any("sin supabase_url" in r.message for r in caplog.records)


# ============================================================
# leads — get/create/update/advance_stage
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_get_lead_by_session_existente() -> None:
    respx.get("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "parent_name": "Ana",
                    "parent_phone": None,
                    "parent_email": None,
                    "child_name": None,
                    "child_age": None,
                    "nivel": None,
                    "channel": "telegram",
                    "classification": None,
                    "stage": "contacto_inicial",
                    "source": "sofia_ai",
                    "conversation_session_id": "telegram:123",
                    "notes": None,
                }
            ],
        )
    )
    lead = await get_lead_by_session("telegram:123", settings=_settings())
    assert isinstance(lead, Lead)
    assert lead.id == 7
    assert lead.parent_name == "Ana"
    assert lead.stage == "contacto_inicial"


@pytest.mark.asyncio
@respx.mock
async def test_get_lead_by_session_no_existe() -> None:
    respx.get("https://x.supabase.co/rest/v1/leads").mock(return_value=httpx.Response(200, json=[]))
    lead = await get_lead_by_session("telegram:999", settings=_settings())
    assert lead is None


@pytest.mark.asyncio
@respx.mock
async def test_create_lead_minimo() -> None:
    respx.post("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(201, json=[{"id": 11}])
    )
    lid = await create_lead(
        parent_name="Ana",
        channel="telegram",
        conversation_session_id="telegram:123",
        settings=_settings(),
    )
    assert lid == 11


@pytest.mark.asyncio
@respx.mock
async def test_create_lead_channel_invalido() -> None:
    """Channel fuera del enum → NO inserta, devuelve None."""
    lid = await create_lead(
        parent_name="Ana",
        channel="instagram_stories_xx",
        conversation_session_id="x:1",
        settings=_settings(),
    )
    assert lid is None


@pytest.mark.asyncio
@respx.mock
async def test_create_lead_nivel_invalido_se_omite() -> None:
    """Nivel fuera del enum se omite silenciosamente."""
    called_payload = {}

    def capture(request):
        import json as _json

        called_payload.update(_json.loads(request.content))
        return httpx.Response(201, json=[{"id": 22}])

    respx.post("https://x.supabase.co/rest/v1/leads").mock(side_effect=capture)
    lid = await create_lead(
        parent_name="Ana",
        channel="telegram",
        conversation_session_id="telegram:1",
        nivel="bachillerato_invalido",
        settings=_settings(),
    )
    assert lid == 22
    assert "nivel" not in called_payload


@pytest.mark.asyncio
@respx.mock
async def test_update_lead_ok() -> None:
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    ok = await update_lead(7, {"parent_email": "ana@example.com"}, settings=_settings())
    assert ok is True


@pytest.mark.asyncio
@respx.mock
async def test_update_lead_stage_invalido_no_envia() -> None:
    """Si solo pasamos stage inválido, no se hace la request."""
    # No agregamos mock: si la función intentara hacer la request, fallaría con
    # respx error (no match). El test pasa precisamente porque NO se intenta.
    ok = await update_lead(7, {"stage": "etapa_inexistente"}, settings=_settings())
    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_advance_stage_avanza_si_target_es_posterior() -> None:
    respx.patch("https://x.supabase.co/rest/v1/leads").mock(
        return_value=httpx.Response(204, text="")
    )
    ok = await advance_stage_if_lower(7, "contacto_inicial", "cita_agendada", settings=_settings())
    assert ok is True


@pytest.mark.asyncio
async def test_advance_stage_no_retrocede() -> None:
    """Si el lead ya está en visita_realizada, no se devuelve a cita_agendada."""
    ok = await advance_stage_if_lower(7, "visita_realizada", "cita_agendada", settings=_settings())
    assert ok is False


@pytest.mark.asyncio
async def test_advance_stage_no_acepta_descartado() -> None:
    """Estados fuera del orden lineal (descartado) no se procesan."""
    ok = await advance_stage_if_lower(7, "descartado", "cita_agendada", settings=_settings())
    assert ok is False


# ============================================================
# appointments — create/get/update
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_ok() -> None:
    respx.post("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(201, json=[{"id": 55}])
    )
    aid = await create_appointment(
        lead_id=10,
        fecha_hora=datetime(2026, 5, 26, 10, 0, tzinfo=TZ_MONTERREY),
        duracion_min=60,
        notas="Solicitada por Sofía",
        settings=_settings(),
    )
    assert aid == 55


@pytest.mark.asyncio
@respx.mock
async def test_create_appointment_status_default_pendiente() -> None:
    """El payload siempre incluye status='pendiente'."""
    called_payload = {}

    def capture(request):
        import json as _json

        called_payload.update(_json.loads(request.content))
        return httpx.Response(201, json=[{"id": 1}])

    respx.post("https://x.supabase.co/rest/v1/appointments").mock(side_effect=capture)
    await create_appointment(
        lead_id=1,
        fecha_hora=datetime(2026, 5, 26, 10, 0, tzinfo=TZ_MONTERREY),
        settings=_settings(),
    )
    assert called_payload["status"] == "pendiente"


@pytest.mark.asyncio
@respx.mock
async def test_get_appointment_ok() -> None:
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 55,
                    "lead_id": 10,
                    "fecha_hora": "2026-05-26T16:00:00+00:00",
                    "duracion_min": 60,
                    "status": "pendiente",
                    "notas": "ok",
                }
            ],
        )
    )
    appt = await get_appointment(55, settings=_settings())
    assert isinstance(appt, Appointment)
    assert appt.id == 55
    assert appt.status == "pendiente"


@pytest.mark.asyncio
@respx.mock
async def test_update_appointment_a_confirmada() -> None:
    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(204, text="")
    )
    ok = await update_appointment(55, {"status": "confirmada"}, settings=_settings())
    assert ok is True


@pytest.mark.asyncio
async def test_update_appointment_status_invalido_no_envia() -> None:
    ok = await update_appointment(55, {"status": "rechazada"}, settings=_settings())
    # 'rechazada' NO está en el enum existente
    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_update_appointment_fecha_hora_serializa_datetime() -> None:
    """Si pasamos un datetime, lo serializa a ISO."""
    captured = {}

    def capture(request):
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(204, text="")

    respx.patch("https://x.supabase.co/rest/v1/appointments").mock(side_effect=capture)
    nueva = datetime(2026, 6, 1, 14, 0, tzinfo=TZ_MONTERREY)
    ok = await update_appointment(55, {"fecha_hora": nueva}, settings=_settings())
    assert ok is True
    assert "2026-06-01" in captured["fecha_hora"]
