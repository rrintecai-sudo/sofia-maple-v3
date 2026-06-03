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
    to: str, subject: str, body: str, *, settings: Settings
) -> EmailPayload:
    """POST a la API de Resend. NUNCA lanza: cualquier error → delivered=False."""
    payload = EmailPayload(to=to, subject=subject, body=body, provider="resend")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
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
    settings: Settings | None = None,
) -> EmailPayload:
    """Envía un email vía Resend (o stub si no hay API key). NUNCA lanza.

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
        return await _send_via_resend(to, subject, body, settings=settings)

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

# Asunto (ajustable).
ASUNTO_CONFIRMACION_PAPA = "Confirmación de tu cita de informes — Maple Collège"

# ⚠️ BORRADOR — reemplazar por el TEXTO LITERAL de Gaby (Mensaje 2 de sus
# capturas). Placeholders disponibles: {nombre_papa} {dia} {hora} {campus}
# {direccion}. El cuerpo es texto plano (emojis OK). Mantén los placeholders.
_PLANTILLA_CONFIRMACION_PAPA = """Hola {nombre_papa} 😊

¡Gracias por agendar tu cita de informes en Maple Collège!

Estos son los detalles de tu visita:

📅 Día: {dia}
🕐 Hora: {hora}
📍 Campus: {campus}
🗺️ Dirección: {direccion}

Te esperamos para platicarte sobre nuestra metodología y resolver todas tus dudas. Si necesitas reagendar, solo responde a este correo y lo coordinamos.

¡Nos vemos pronto!
Equipo de Admisiones — Maple Collège"""


def render_confirmacion_email_papa(
    *,
    nombre_papa: str | None,
    fecha_hora,  # datetime aware (America/Monterrey) — se formatea como D.4
    campus,  # CampusResult | None
) -> tuple[str, str]:
    """(subject, body) del correo de confirmación al PAPÁ. Reutiliza el formato de
    fecha/hora/dirección de D.4 (Gaby) para que coincida con el cierre en chat."""
    # Import local para evitar ciclo (appointment_messages importa de campus).
    from app.core.appointment_messages import formato_dia_fecha, formato_hora

    nombre = (nombre_papa or "").strip() or "papá/mamá"
    dia = formato_dia_fecha(fecha_hora)
    hora = formato_hora(fecha_hora)
    nombre_campus = campus.nombre if campus else "nuestro campus"
    direccion = campus.direccion_legible() if campus else "te compartimos la dirección por separado"

    body = _PLANTILLA_CONFIRMACION_PAPA.format(
        nombre_papa=nombre, dia=dia, hora=hora, campus=nombre_campus, direccion=direccion
    )
    return ASUNTO_CONFIRMACION_PAPA, body
