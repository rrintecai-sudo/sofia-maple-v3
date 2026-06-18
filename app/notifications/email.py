"""Notificación por email (Bloque C.1).

Dos usos:
- Aviso interno a Lily de cita pendiente (`render_cita_pendiente_email`).
- Correo de CONFIRMACIÓN al papá (`render_confirmacion_email_papa`, Mensaje 2 de
  Gaby), enviado al crear la cita.

Provider: Resend vía HTTP (sin dependencia nueva — usa httpx). Si
`settings.resend_api_key` está vacío, cae al stub que solo loggea.

PRINCIPIO: el correo NUNCA es load-bearing. `send_email` NUNCA lanza — captura
cualquier error de red/Resend, lo loggea y devuelve `delivered=False`. La cita y
el cierre D.4 se hacen igual aunque el correo falle.
"""

from __future__ import annotations

import html as _html
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass
class EmailPayload:
    to: str
    subject: str
    body: str
    delivered: bool = False  # True si Resend lo aceptó
    provider: str = "stub"  # 'stub' | 'resend'
    provider_id: str | None = None  # id que devuelve Resend
    error: str | None = None  # error si falló (NO se relanza)


async def _send_via_resend(
    to: str, subject: str, body: str, *, html: str | None = None, settings: Settings
) -> EmailPayload:
    """POST a la API de Resend. NUNCA lanza: cualquier error → delivered=False.

    Envía `text` siempre y `html` si se provee (clientes prefieren HTML cuando
    existe; el texto queda como fallback).
    """
    payload = EmailPayload(to=to, subject=subject, body=body, provider="resend")
    cuerpo = {"from": settings.email_from, "to": [to], "subject": subject, "text": body}
    if html:
        cuerpo["html"] = html
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                    # Cloudflare (frente a Resend) bloquea el User-Agent por defecto de
                    # httpx con error 1010 → el correo se rechazaba en silencio. Un UA de
                    # navegador pasa el filtro de integridad. (Bug real: sin esto NO se
                    # envía ningún correo, ni en prod.)
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                },
                json=cuerpo,
            )
        if resp.status_code in (200, 201):
            payload.delivered = True
            payload.provider_id = resp.json().get("id")
            log.info("email_resend_sent", extra={"to": to, "id": payload.provider_id})
        else:
            payload.error = f"http_{resp.status_code}: {resp.text[:200]}"
            log.warning(
                "email_resend_rejected",
                extra={"to": to, "status": resp.status_code, "body": resp.text[:200]},
            )
    except Exception as exc:  # red caída, timeout, etc. — NO load-bearing
        payload.error = str(exc)
        log.warning("email_resend_error", extra={"to": to, "error": str(exc)})
    return payload


async def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html: str | None = None,
    settings: Settings | None = None,
) -> EmailPayload:
    """Envía un email vía Resend (o stub si no hay API key). NUNCA lanza.

    Args:
        body: cuerpo en texto plano (fallback).
        html: cuerpo HTML opcional (clientes lo prefieren; el correo de
            confirmación al papá lo usa para el link clickeable de Maps).

    Returns:
        EmailPayload con `delivered` True/False. El caller puede ignorarlo: el
        correo es complementario, nunca bloquea el agendado.
    """
    settings = settings or get_settings()

    if not to:
        log.warning(
            "email_skip_destinatario_vacio",
            extra={"subject": subject, "body_preview": body[:120]},
        )
        return EmailPayload(to=to, subject=subject, body=body)

    if settings.resend_api_key:
        return await _send_via_resend(to, subject, body, html=html, settings=settings)

    # Sin API key → stub: log estructurado, auditable en producción.
    log.warning(
        "email_stub_send",
        extra={
            "to": to,
            "subject": subject,
            "body": body,
            "provider": "stub",
            "note": "RESEND_API_KEY vacío — solo log (no se envió correo real)",
        },
    )
    return EmailPayload(to=to, subject=subject, body=body)


def render_cita_pendiente_email(
    *,
    nombre_papa: str | None,
    nombre_hijo: str | None,
    edad_hijo: int | None,
    nivel: str | None,
    fecha_hora_iso: str,
    canal: str,
    appointment_id: int,
    approval_url: str | None = None,
) -> tuple[str, str]:
    """Construye (subject, body) del email a Lily para nueva cita pendiente.

    Mantenemos el template aquí (no en string template) para que sea
    fácilmente testeable.
    """
    nombre = (nombre_papa or "Papá/mamá").strip() or "Papá/mamá"
    subject = f"Nueva visita pendiente: {nombre} — {fecha_hora_iso}"

    lineas: list[str] = []
    lineas.append("Sofía registró una solicitud de visita pendiente de tu aprobación.")
    lineas.append("")
    lineas.append("Detalles:")
    lineas.append(f"- Papá/mamá: {nombre}")
    if nombre_hijo:
        edad_str = f", {edad_hijo} años" if edad_hijo is not None else ""
        lineas.append(f"- Hijo: {nombre_hijo}{edad_str}")
    elif edad_hijo is not None:
        lineas.append(f"- Edad del hijo: {edad_hijo} años")
    if nivel:
        lineas.append(f"- Nivel de interés: {nivel}")
    lineas.append(f"- Fecha solicitada: {fecha_hora_iso}")
    lineas.append(f"- Canal de conversación: {canal}")
    lineas.append(f"- Appointment ID: {appointment_id}")
    if approval_url:
        lineas.append("")
        lineas.append(f"Aprobar o rechazar en la plataforma: {approval_url}")
    lineas.append("")
    lineas.append("— Sofía")

    return subject, "\n".join(lineas)


# ============================================================
# Correo de CONFIRMACIÓN al papá — Mensaje 2 de Gaby (Bloque C.1)
# ============================================================

# Asunto.
ASUNTO_CONFIRMACION_PAPA = "Confirmación de tu cita de informes — Maple Collège"

# Texto LITERAL de Gaby (Mensaje 2). El correo es HTML (Maps clickeable); también
# se envía un fallback en texto plano con la URL cruda.
_INTRO_CONFIRMACION = (
    "Te confirmamos tu cita de informes para conocer Maple Collège y platicarte con "
    "más detalle sobre nuestra metodología, acompañamiento académico y filosofía "
    "educativa."
)
_CIERRE_CONFIRMACION = (
    "Durante la visita podremos resolver todas sus dudas, compartirles información "
    "importante sobre el proceso y hacer un recorrido por las instalaciones."
)
# CIERRE final: Gaby no dio última línea → se mantiene la del borrador.
_DESPEDIDA_CONFIRMACION = "¡Te esperamos! 😊\nEquipo de Admisiones — Maple Collège"


def render_confirmacion_email_papa(
    *,
    nombre_papa: str | None,
    fecha_hora,  # datetime aware (America/Monterrey) — se formatea como D.4
    campus,  # CampusResult | None
) -> tuple[str, str, str]:
    """(subject, body_text, body_html) del correo de confirmación al PAPÁ.

    Reutiliza el formato de fecha/hora/dirección de D.4 (Gaby). El link de Maps
    sale de `campus.google_maps_url` (misma fuente de tabla que D.4, NO inventado):
    en HTML va como <a> clickeable; en el texto plano, la URL cruda.
    """
    # Import local para evitar ciclo (appointment_messages importa de campus).
    from app.core.appointment_messages import formato_dia_fecha, formato_hora

    nombre = (nombre_papa or "").strip() or "papá/mamá"
    dia = formato_dia_fecha(fecha_hora)
    hora = formato_hora(fecha_hora)
    nombre_campus = campus.nombre if campus else "nuestro campus"
    direccion = campus.direccion_legible() if campus else "te compartimos la dirección por separado"
    maps_url = (campus.google_maps_url if campus else None) or ""

    # --- Texto plano (fallback): Maps como URL cruda ---
    text_lineas = [
        f"Hola {nombre} 😊",
        "",
        _INTRO_CONFIRMACION,
        "",
        f"📅 Día: {dia}",
        f"🕐 Hora: {hora}",
        f"📍 Campus: {nombre_campus}",
        f"📌 Dirección: {direccion}",
    ]
    if maps_url:
        text_lineas.append(f"🗺️ {maps_url}")
    text_lineas += ["", _CIERRE_CONFIRMACION, "", _DESPEDIDA_CONFIRMACION]
    body_text = "\n".join(text_lineas)

    # --- HTML: Maps como enlace clickeable. Se escapan los valores dinámicos. ---
    def esc(s: object) -> str:
        return _html.escape(str(s))

    maps_html = f'🗺️ <a href="{esc(maps_url)}">Ver ubicación en Google Maps</a>' if maps_url else ""
    body_html = (
        '<!DOCTYPE html><html><body style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:15px;color:#222;line-height:1.5;">'
        f"<p>Hola {esc(nombre)} 😊</p>"
        f"<p>{esc(_INTRO_CONFIRMACION)}</p>"
        "<p>"
        f"📅 <strong>Día:</strong> {esc(dia)}<br>"
        f"🕐 <strong>Hora:</strong> {esc(hora)}<br>"
        f"📍 <strong>Campus:</strong> {esc(nombre_campus)}<br>"
        f"📌 <strong>Dirección:</strong> {esc(direccion)}<br>"
        f"{maps_html}"
        "</p>"
        f"<p>{esc(_CIERRE_CONFIRMACION)}</p>"
        "<p>¡Te esperamos! 😊<br>Equipo de Admisiones — Maple Collège</p>"
        "</body></html>"
    )
    return ASUNTO_CONFIRMACION_PAPA, body_text, body_html
