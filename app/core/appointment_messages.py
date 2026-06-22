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


def _maps_line(google_maps_url: str | None, canal: str | None) -> str | None:
    """FIX 2 (2026-06-01): el link de Maps como hipervínculo según el canal.

    - web / telegram: markdown clickeable con texto amigable.
    - whatsapp (u otro/None): URL cruda (WhatsApp la vuelve clickeable nativo;
      el markdown no aplica ahí).
    """
    if not google_maps_url:
        return None
    if canal in ("web", "telegram"):
        return f"🗺️ [Ver ubicación en Google Maps]({google_maps_url})"
    return f"🗺️ {google_maps_url}"


# ============================================================
# Preguntas de COLECCIÓN determinísticas (2026-06-04). La pregunta de cada campo
# la genera el CÓDIGO con plantilla FIJA (un solo campo), no Haiku → no puede
# bundlear, reordenar ni improvisar wording ("solicitud"). El override en el
# orchestrator las usa salvo que el papá haga una pregunta sustantiva.
# ============================================================


# Aclaración de FORMATO por campo, para el reintento (guard anti-bucle): si la
# respuesta no se pudo parsear, se re-formula con un ejemplo en vez de repetir
# idéntica la misma pregunta.
_EJEMPLO_FORMATO: dict[str, str] = {
    "dia": "puedes decir 'mañana', 'el jueves' o una fecha como '12 de junio'",
    "hora": "por ejemplo '10am', '1pm' o 'a las 11'",
    "edad": "solo el número de años, por ejemplo '5'",
    "nombre_hijo": "su nombre y apellido",
    "nombre_papa": "tu nombre completo",
    "correo": "algo como nombre@correo.com",
    "telefono": "tus 10 dígitos",
}


def _join_o(partes: list[str]) -> str:
    """['a','b','c'] → 'a, b o c'."""
    if not partes:
        return ""
    if len(partes) == 1:
        return partes[0]
    return ", ".join(partes[:-1]) + f" o {partes[-1]}"


def prep_dia(dia: str) -> str:
    """Preposición correcta antes de `dia`: 'del miércoles 17' pero 'de hoy, miércoles
    17' / 'de mañana, jueves 18' (la etiqueta hoy/mañana no admite 'del')."""
    d = (dia or "").lower().lstrip()
    return "de" if d.startswith(("hoy", "mañana")) else "del"


def art_dia(dia: str) -> str:
    """Artículo correcto antes de `dia`: 'el miércoles 17' pero '' para 'hoy,…'/'mañana,…'
    (decir 'el hoy' es agramatical). Devuelve 'el ' o '' (listo para concatenar)."""
    d = (dia or "").lower().lstrip()
    return "" if d.startswith(("hoy", "mañana")) else "el "


def _etiqueta_relativa(f: datetime, now: datetime | None) -> str:
    """'hoy'/'mañana' si la fecha cae hoy/mañana respecto a `now`, si no ''."""
    if now is None:
        return ""
    hoy = _to_monterrey(now).date()
    d = f.date()
    if d == hoy:
        return "hoy, "
    if (d - hoy).days == 1:
        return "mañana, "
    return ""


def formato_opciones_dia(fechas: list[datetime], now: datetime | None = None) -> str:
    """[jueves 11, viernes 12, lunes 15] → 'jueves 11, viernes 12 o lunes 15 de junio'.
    Si una fecha es hoy/mañana, la etiqueta ('hoy, lunes 15')."""
    if not fechas:
        return ""
    mismo_mes = len({(f.year, f.month) for f in fechas}) == 1
    if mismo_mes:
        partes = [f"{_etiqueta_relativa(f, now)}{_DIAS_ES[f.weekday()]} {f.day}" for f in fechas]
        return f"{_join_o(partes)} de {_MESES_ES[fechas[0].month - 1]}"
    partes = [
        f"{_etiqueta_relativa(f, now)}{_DIAS_ES[f.weekday()]} {f.day} de {_MESES_ES[f.month - 1]}"
        for f in fechas
    ]
    return _join_o(partes)


def render_pregunta_campo(
    campo: str,
    *,
    nombre_hijo: str | None = None,
    dia: str | None = None,
    horario: str | None = None,
    horas_libres: str | None = None,
    opciones_dia: str | None = None,
    motivo: str | None = None,
    reintento: bool = False,
) -> str | None:
    """Pregunta FIJA por el único `campo` que falta. None si el campo no tiene
    plantilla (el caller cae a Haiku con el hint).

    `motivo`: explicación corta que se antepone (ej. el día pedido no se puede).
    `reintento=True`: la respuesta anterior no se parseó → re-formula con el formato
    esperado (NUNCA se repite idéntica la misma línea — guard anti-bucle)."""
    hijo = nombre_hijo or "tu peque"
    linea_horario = f" {horario}" if horario else ""
    if campo == "dia":
        if opciones_dia:
            # El código PROPONE fechas concretas (no abre a parseo ambiguo de "hoy").
            base = f"¿Qué día te queda mejor? Tengo disponible {opciones_dia}."
        else:
            base = ("¿Qué día te queda mejor para tu visita?" + linea_horario).strip()
    elif campo == "hora":
        base = (
            f"¿A qué hora {prep_dia(dia)} {dia} te viene bien?"
            if dia
            else "¿A qué hora te viene bien?"
        )
        if horas_libres:
            base = f"{base} Ese día tenemos disponibles: {horas_libres}."
        else:
            base = base + linea_horario
    else:
        plantillas = {
            "nombre_hijo": "Para agendar tu cita, ¿me confirmas el nombre completo de tu hijo/a? 😊",
            "edad": f"¿Y qué edad tiene {hijo}?",
            "grado": f"¿En qué grado está {hijo}?",
            "nombre_papa": "Perfecto. ¿Y cuál es tu nombre completo?",
            "correo": "Gracias. ¿Me compartes tu correo electrónico para enviarte la confirmación?",
            "telefono": "Y por último, ¿me das tu número de celular?",
        }
        base = plantillas.get(campo)
    if base is None:
        return None
    if motivo:
        base = f"{motivo} {base}"
    if reintento:
        # Re-pregunta CÁLIDA: guía el formato SIN "Perdón, no te entendí bien" (suena a
        # regaño y molestaba — queja de Gaby). El ejemplo orienta sin culpar al papá.
        ejemplo = _EJEMPLO_FORMATO.get(campo)
        return f"{base}" + (f" Puedes ponerlo así: {ejemplo} 😊" if ejemplo else "")
    return base


def render_registration_message(
    *,
    fecha_hora: datetime,
    campus: CampusResult | None,
    canal: str | None = None,
    now: datetime | None = None,
) -> str:
    """Mensaje que Sofía envía cuando la cita queda REGISTRADA como pendiente.

    Texto oficial de Gaby (reunión 27-may). Determinístico — NO depende del LLM.
    El link de Maps se renderiza según `canal` (FIX 2). Si la fecha es hoy/mañana,
    se etiqueta ("hoy, miércoles 17 de junio") usando `now` (hora de Saltillo).
    """
    dia = _etiqueta_relativa(fecha_hora, now) + formato_dia_fecha(fecha_hora)
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
    maps = _maps_line(campus.google_maps_url if campus else None, canal)
    if maps:
        lineas.append(maps)
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
    canal: str | None = None,
) -> str:
    """Mensaje cuando Lily APRUEBA la cita (POST /api/appointments/{id}/approve).

    Mismo formato visual que el de registro, pero con texto de confirmación.
    El link de Maps se renderiza según `canal` (FIX 2).
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
    maps = _maps_line(campus.google_maps_url if campus else None, canal)
    if maps:
        lineas.append(maps)
    lineas.extend(
        [
            "",
            "Te esperamos. Si necesitas reagendar, escríbeme y lo coordinamos.",
        ]
    )
    return "\n".join(lineas)
