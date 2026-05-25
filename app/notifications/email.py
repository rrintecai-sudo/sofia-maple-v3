"""Notificación por email a Lily (Bloque C.1 PASO 6).

Stub que loggea con structlog/logging nivel WARNING. NO envía email real.
Maple Platform consume `activity_events` para mostrar la notificación
visualmente en su dashboard — el email es complementario.

TODO (Bloque C.X): integrar Resend cuando Cecilia confirme el dominio
de email para Maple. Cuando se haga:
- añadir RESEND_API_KEY a Settings
- pip add resend
- reemplazar el stub en _send_via_provider() por la llamada real
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass
class EmailPayload:
    to: str
    subject: str
    body: str
    delivered: bool = False  # True si llegó a un provider real
    provider: str = "stub"  # 'stub' | 'resend' (en el futuro)


async def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    settings: Settings | None = None,
) -> EmailPayload:
    """Envía un email. Hoy stub: loggea con nivel WARNING (visible en logs).

    Args:
        to: email destino. Si está vacío, no se loggea como envío fallido —
            el caller decide si quiere lanzar.
        subject: asunto
        body: cuerpo (texto plano o HTML según futuro provider)

    Returns:
        EmailPayload con `delivered=False` y `provider='stub'`. El caller
        puede usarlo para tracking (ej. guardar en activity_events.metadata).
    """
    settings = settings or get_settings()

    payload = EmailPayload(to=to, subject=subject, body=body)

    if not to:
        log.warning(
            "email_stub_skip_destinatario_vacio",
            extra={"subject": subject, "body_preview": body[:120]},
        )
        return payload

    # TODO Bloque C.X: integrar Resend.
    # Por ahora: log estructurado con todo el payload para que sea
    # auditable en producción mientras Maple Platform muestra la
    # notificación al usuario humano.
    log.warning(
        "email_stub_send",
        extra={
            "to": to,
            "subject": subject,
            "body": body,
            "provider": payload.provider,
            "delivered": payload.delivered,
            "note": "stub — integrar Resend cuando Cecilia confirme dominio (TODO Bloque C.X)",
        },
    )
    return payload


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
