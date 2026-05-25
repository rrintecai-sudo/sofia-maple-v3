"""Helper para emitir eventos a la tabla `activity_events` (Bloque C.1 PASO 6).

Maple Platform consume estos eventos para mostrar timeline + campana
de notificaciones en su dashboard. Sofía siempre actúa como `actor_type='sofia'`.

Event types canónicos (deben existir en el enum public.event_type ya
creado por Maple Platform):
- lead_created
- lead_stage_changed
- lead_note_added
- lead_assigned
- sofia_classified
- sofia_appointment_scheduled
- sofia_escalated
- sofia_error
- appointment_created

Esta función es resiliente: si Supabase falla, loggea pero NO levanta —
el flujo principal no se rompe por un fallo de auditoría.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

# Event types que aceptamos (espejo del enum en Postgres). Si llega uno
# que no está en la lista, igual lo enviamos — Postgres rechazará y
# loggeamos. Mantenemos esta constante como referencia documental.
EVENT_TYPES_SOFIA = (
    "sofia_classified",
    "sofia_appointment_scheduled",
    "sofia_escalated",
    "sofia_error",
    "lead_created",
    "lead_stage_changed",
    "lead_note_added",
    "lead_assigned",
    "appointment_created",
)


async def emit_event(
    event_type: str,
    *,
    lead_id: int | None = None,
    session_id: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_type: str = "sofia",
    settings: Settings | None = None,
) -> int | None:
    """Inserta una fila en `activity_events`.

    Args:
        event_type: uno de los enum values existentes en la BD.
        lead_id: opcional (algunos eventos pre-existen al lead).
        session_id: opcional (NO existe como columna en activity_events;
            si se pasa, se mete dentro de metadata.session_id).
        description: descripción breve para humanos.
        metadata: JSONB libre.
        actor_type: 'sofia' o 'user'.

    Returns:
        id del evento insertado, o None si falló.
    """
    settings = settings or get_settings()
    if not settings.supabase_url:
        log.warning("emit_event sin supabase_url", extra={"event_type": event_type})
        return None

    meta = dict(metadata or {})
    if session_id:
        meta.setdefault("session_id", session_id)

    payload: dict[str, Any] = {
        "event_type": event_type,
        "actor_type": actor_type,
        "metadata": meta,
    }
    if lead_id is not None:
        payload["lead_id"] = lead_id
    if description is not None:
        payload["description"] = description[:1000]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.supabase_url}/rest/v1/activity_events",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                },
                json=payload,
            )
    except Exception as exc:
        log.warning(
            "emit_event httpx_error",
            extra={"event_type": event_type, "error": str(exc)},
        )
        return None

    if resp.status_code >= 400:
        log.warning(
            "emit_event http_error",
            extra={
                "event_type": event_type,
                "status": resp.status_code,
                "body": resp.text[:300],
            },
        )
        return None

    try:
        rows = resp.json()
    except Exception:
        return None

    if rows and isinstance(rows, list):
        return rows[0].get("id")
    return None
