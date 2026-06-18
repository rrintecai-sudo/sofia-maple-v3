"""Tests de los helpers de mensajes determinísticos al papá (D.4, Gaby 2026-05-27)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.appointment_messages import (
    _maps_line,
    formato_dia_fecha,
    formato_hora,
    render_confirmation_message,
    render_registration_message,
)
from app.tools.campus import CampusResult

TZ_MTY = ZoneInfo("America/Monterrey")
_URL = "https://www.google.com/maps/search/?api=1&query=x"


# ============================================================
# FIX 2 (2026-06-01) — link de Maps como hipervínculo por canal
# ============================================================


def test_maps_line_web_y_telegram_markdown() -> None:
    for canal in ("web", "telegram"):
        linea = _maps_line(_URL, canal)
        assert linea == f"🗺️ [Ver ubicación en Google Maps]({_URL})"


def test_maps_line_whatsapp_url_cruda() -> None:
    assert _maps_line(_URL, "whatsapp") == f"🗺️ {_URL}"


def test_maps_line_canal_desconocido_url_cruda() -> None:
    assert _maps_line(_URL, None) == f"🗺️ {_URL}"


def test_maps_line_sin_url() -> None:
    assert _maps_line(None, "web") is None


def test_render_registration_web_lleva_hipervinculo() -> None:
    from app.core.appointment_messages import render_registration_message as rr

    dt = datetime(2026, 6, 3, 11, 0, tzinfo=TZ_MTY)
    msg = rr(fecha_hora=dt, campus=_campus_1(), canal="web")
    assert "[Ver ubicación en Google Maps](" in msg


def test_render_registration_whatsapp_url_cruda() -> None:
    from app.core.appointment_messages import render_registration_message as rr

    dt = datetime(2026, 6, 3, 11, 0, tzinfo=TZ_MTY)
    msg = rr(fecha_hora=dt, campus=_campus_1(), canal="whatsapp")
    assert "[Ver ubicación" not in msg
    assert "https://www.google.com/maps" in msg


def _campus_1() -> CampusResult:
    return CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1"],
        google_maps_url=(
            "https://www.google.com/maps/search/?api=1&query=Jos%C3%A9+Figueroa+Siller+156"
        ),
    )


def test_formato_dia_fecha_humano() -> None:
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)  # jueves
    assert formato_dia_fecha(dt) == "jueves 4 de junio de 2026"


def test_formato_dia_fecha_convierte_utc_a_monterrey() -> None:
    """Si llega un datetime en UTC, lo convierte antes de formatear."""
    utc = datetime(2026, 6, 4, 16, 0, tzinfo=ZoneInfo("UTC"))
    # En Monterrey son las 10:00 a.m.
    s = formato_dia_fecha(utc)
    assert "jueves 4 de junio" in s


def test_formato_hora_am() -> None:
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    assert formato_hora(dt) == "10:00 a.m."


def test_formato_hora_pm() -> None:
    dt = datetime(2026, 6, 4, 15, 30, tzinfo=TZ_MTY)
    assert formato_hora(dt) == "3:30 p.m."


def test_formato_hora_medianoche_y_mediodia() -> None:
    medianoche = datetime(2026, 6, 4, 0, 0, tzinfo=TZ_MTY)
    mediodia = datetime(2026, 6, 4, 12, 0, tzinfo=TZ_MTY)
    assert formato_hora(medianoche) == "12:00 a.m."
    assert formato_hora(mediodia) == "12:00 p.m."


# ============================================================
# render_registration_message — copy oficial de Gaby
# ============================================================


def test_registration_message_estructura_oficial() -> None:
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    msg = render_registration_message(fecha_hora=dt, campus=_campus_1())

    # Apertura oficial
    assert msg.startswith("Listo, ya quedó agendada tu cita de informes 😊")
    # Bloques con emojis en orden
    assert "📅 Día: jueves 4 de junio de 2026" in msg
    assert "🕐 Hora: 10:00 a.m." in msg
    assert "📍 Campus: Campus 1" in msg
    assert "🗺️ Dirección: José Figueroa Siller 156, Col. Doctores, Saltillo, Coah." in msg
    # Link Google Maps presente
    assert "https://www.google.com/maps" in msg
    # Cierre oficial
    assert "En breve te confirmamos por este mismo medio" in msg
    assert "✨" in msg


def test_registration_message_sin_campus_no_revienta() -> None:
    """Si no hay campus resuelto (caso edge), el mensaje sigue saliendo
    sin link de Maps pero con la estructura visual."""
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    msg = render_registration_message(fecha_hora=dt, campus=None)
    assert "Listo, ya quedó agendada" in msg
    assert "📅 Día" in msg and "🕐 Hora" in msg
    assert "https://www.google.com/maps" not in msg


def test_registration_message_sin_maps_url() -> None:
    """Si el campus existe pero no tiene google_maps_url (no debería pasar en
    prod, pero defendamos), el mensaje sale sin el link."""
    campus = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=[],
        google_maps_url=None,
    )
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    msg = render_registration_message(fecha_hora=dt, campus=campus)
    assert "📍 Campus: Campus 1" in msg
    assert "https://www.google.com/maps" not in msg


# ============================================================
# render_confirmation_message — Lily aprueba
# ============================================================


def test_confirmation_message_con_nombre_papa() -> None:
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    msg = render_confirmation_message(fecha_hora=dt, campus=_campus_1(), nombre_papa="Ana")
    assert msg.startswith("¡Listo, Ana! Lily confirmó tu cita de informes")
    assert "📅" in msg and "🕐" in msg and "📍" in msg and "🗺️" in msg
    assert "https://www.google.com/maps" in msg


def test_confirmation_message_sin_nombre_papa() -> None:
    dt = datetime(2026, 6, 4, 10, 0, tzinfo=TZ_MTY)
    msg = render_confirmation_message(fecha_hora=dt, campus=_campus_1(), nombre_papa=None)
    assert msg.startswith("¡Listo! Lily confirmó tu cita de informes")
