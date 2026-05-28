"""Mensajes determinísticos al papá sobre su cita (D.4 — Gaby 2026-05-27).

El LLM se equivocaba al omitir el link de Maps o la dirección, aún con el
hint indicándole copia-pega. Estos mensajes son TEMPLATES literales — el
orchestrator los inyecta como respuesta final (override del LLM) cuando
se registra una cita pendiente o cuando Lily aprueba.

Copy oficial pasado por Gaby en la reunión 27-may.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.tools.campus import CampusResult

TZ_MONTERREY = ZoneInfo("America/Monterrey")

_DIAS_ES = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)
_MESES_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def _to_monterrey(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MONTERREY)
    return dt.astimezone(TZ_MONTERREY)


def formato_dia_fecha(dt: datetime) -> str:
    """`miércoles 4 de junio de 2026` — bloque del campo 📅 Día."""
    dt = _to_monterrey(dt)
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]} de {dt.year}"


def formato_hora(dt: datetime) -> str:
    """`10:00 a.m.` / `3:00 p.m.` — bloque del campo 🕐 Hora.

    Se usa formato 12h con am/pm en minúsculas con puntos (estilo local mexicano).
    Acepta horas 00-23, las convierte.
    """
    dt = _to_monterrey(dt)
    hora_24 = dt.hour
    minuto = dt.minute
    if hora_24 == 0:
        h12 = 12
        sufijo = "a.m."
    elif hora_24 < 12:
        h12 = hora_24
        sufijo = "a.m."
    elif hora_24 == 12:
        h12 = 12
        sufijo = "p.m."
    else:
        h12 = hora_24 - 12
        sufijo = "p.m."
    return f"{h12}:{minuto:02d} {sufijo}"


def render_registration_message(
    *,
    fecha_hora: datetime,
    campus: CampusResult | None,
) -> str:
    """Mensaje que Sofía envía cuando la cita queda REGISTRADA como pendiente.

    Texto oficial de Gaby (reunión 27-may). Determinístico — NO depende del LLM.
    """
    dia = formato_dia_fecha(fecha_hora)
    hora = formato_hora(fecha_hora)
    nombre_campus = campus.nombre if campus else "nuestro campus"
    direccion = campus.direccion_legible() if campus else "te paso la dirección por separado"

    lineas: list[str] = [
        "Listo, ya quedó agendada tu cita de informes 😊",
        "",
        f"📅 Día: {dia}",
        f"🕐 Hora: {hora}",
        f"📍 Campus: {nombre_campus}",
        f"🗺️ Dirección: {direccion}",
    ]
    if campus and campus.google_maps_url:
        lineas.append(campus.google_maps_url)
    lineas.extend(
        [
            "",
            "En breve te confirmamos por este mismo medio. Si surge cualquier duda, "
            "aquí quedo pendiente ✨",
        ]
    )
    return "\n".join(lineas)


def render_confirmation_message(
    *,
    fecha_hora: datetime,
    campus: CampusResult | None,
    nombre_papa: str | None = None,
) -> str:
    """Mensaje cuando Lily APRUEBA la cita (POST /api/appointments/{id}/approve).

    Mismo formato visual que el de registro, pero con texto de confirmación.
    """
    dia = formato_dia_fecha(fecha_hora)
    hora = formato_hora(fecha_hora)
    nombre_campus = campus.nombre if campus else "nuestro campus"
    direccion = campus.direccion_legible() if campus else "te paso la dirección por separado"

    encabezado = (
        f"¡Listo, {nombre_papa}! Lily confirmó tu cita de informes 🎉"
        if nombre_papa
        else "¡Listo! Lily confirmó tu cita de informes 🎉"
    )
    lineas: list[str] = [
        encabezado,
        "",
        f"📅 Día: {dia}",
        f"🕐 Hora: {hora}",
        f"📍 Campus: {nombre_campus}",
        f"🗺️ Dirección: {direccion}",
    ]
    if campus and campus.google_maps_url:
        lineas.append(campus.google_maps_url)
    lineas.extend(
        [
            "",
            "Te esperamos. Si necesitas reagendar, escríbeme y lo coordinamos.",
        ]
    )
    return "\n".join(lineas)
