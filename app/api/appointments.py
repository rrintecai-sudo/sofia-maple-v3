"""Endpoints para que Maple Platform apruebe/rechace citas (Bloque C.1 PASO 7).

Lily aprueba o rechaza desde la plataforma (UI a construir aparte).
Estos endpoints son llamados por la plataforma — protegidos por
X-Admin-Key (mismo patrón que admin.py).

Endpoints:
  POST /api/appointments/{id}/approve
  POST /api/appointments/{id}/reject

Ambos:
  - Validan que la cita exista y esté en 'pendiente'
  - Cambian el status
  - Emiten activity_event
  - Envían mensaje al papá por el canal donde conversó (Telegram /
    WhatsApp / Web)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.adapters.dispatcher import send_message_to_session
from app.config import get_settings
from app.core.appointment_extractor import TZ_MONTERREY
from app.core.appointment_messages import render_confirmation_message
from app.integrations.appointments import (
    get_appointment,
    update_appointment,
)
from app.integrations.events import emit_event
from app.integrations.leads import (
    advance_stage_if_lower,
    get_lead_by_session,
    update_lead,
)
from app.tools.campus import CampusResult, get_campus_by_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


# ============================================================
# Auth
# ============================================================


def _check_admin(x_admin_key: str | None) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        return  # modo dev — sin protección
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")


# ============================================================
# Modelos
# ============================================================


class ApproveIn(BaseModel):
    approved_by: str | None = None  # 'lily' por default; útil para auditoría


class RejectIn(BaseModel):
    alternative_datetime: str | None = None  # ISO string si propone otra fecha
    reason: str | None = None


# ============================================================
# Helpers
# ============================================================


def _formato_fecha_humana(dt: datetime) -> str:
    # Supabase devuelve TIMESTAMPTZ con offset UTC; convertimos a Monterrey
    # para mostrar la hora local que el papá entiende (UTC-6 estándar).
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_MONTERREY)
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
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
    ]
    return f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


async def _session_id_de_appointment(appointment_lead_id: int) -> str | None:
    """Obtiene el session_id del lead de esta cita (para mandar mensaje)."""
    # No tenemos un get_lead_by_id porque siempre fluimos por session_id;
    # consultamos directo a la tabla.
    import httpx

    settings = get_settings()
    if not settings.supabase_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/leads",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "id": f"eq.{appointment_lead_id}",
                    "select": "conversation_session_id",
                    "limit": "1",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_session_id_de_appointment failed", extra={"error": str(exc)})
        return None
    if not rows:
        return None
    return rows[0].get("conversation_session_id")


# ============================================================
# POST /api/appointments/{id}/approve
# ============================================================


@router.post("/{appointment_id}/approve")
async def approve_appointment(
    appointment_id: int,
    body: ApproveIn,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    _check_admin(x_admin_key)

    appt = await get_appointment(appointment_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="appointment not found")
    if appt.status != "pendiente":
        raise HTTPException(
            status_code=409,
            detail=f"appointment ya está en estado '{appt.status}', no se puede aprobar",
        )

    ok = await update_appointment(appointment_id, {"status": "confirmada"})
    if not ok:
        raise HTTPException(status_code=500, detail="no se pudo actualizar la cita")

    session_id = await _session_id_de_appointment(appt.lead_id)

    # Avanza stage si aplica + emit event
    if session_id:
        lead = await get_lead_by_session(session_id)
        if lead and lead.stage != "cita_agendada":
            await advance_stage_if_lower(lead.id, lead.stage, "cita_agendada")
        # Si ya está en cita_agendada, no avanzamos. La visita real cambia
        # a 'visita_realizada' después.

    fecha_humana = _formato_fecha_humana(appt.fecha_hora)
    actor_id_metadata: dict[str, Any] = {
        "appointment_id": appointment_id,
        "from_status": "pendiente",
        "to_status": "confirmada",
        "approved_by": body.approved_by or "lily",
    }
    await emit_event(
        "appointment_created",
        lead_id=appt.lead_id,
        session_id=session_id,
        description=f"Cita confirmada por {body.approved_by or 'Lily'} para {fecha_humana}",
        metadata=actor_id_metadata,
    )

    # Resolver campus (de la cita si lo tiene; si no, del lead.nivel)
    campus: CampusResult | None = None
    if appt.campus_id is not None:
        campus = await get_campus_by_id(appt.campus_id)

    # Mensaje al papá — texto determinístico D.4 (Gaby 2026-05-27).
    # Mismo formato (📅/🕐/📍/🗺️) que el de registro pero con copy de
    # confirmación. NO depende del LLM.
    message_sent: dict[str, Any] = {"sent": False, "channel": None}
    if session_id:
        nombre_papa = None
        lead_msg = await get_lead_by_session(session_id)
        if lead_msg:
            nombre_papa = lead_msg.parent_name
        texto = render_confirmation_message(
            fecha_hora=appt.fecha_hora,
            campus=campus,
            nombre_papa=nombre_papa,
            canal=session_id.split(":", 1)[0] if session_id else None,  # FIX 2
        )
        message_sent = await send_message_to_session(session_id, texto)

    return {
        "success": True,
        "appointment_id": appointment_id,
        "status": "confirmada",
        "fecha_hora": appt.fecha_hora.isoformat(),
        "session_id": session_id,
        "message_sent": message_sent,
    }


# ============================================================
# POST /api/appointments/{id}/reject
# ============================================================


@router.post("/{appointment_id}/reject")
async def reject_appointment(
    appointment_id: int,
    body: RejectIn,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    _check_admin(x_admin_key)

    appt = await get_appointment(appointment_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="appointment not found")
    if appt.status != "pendiente":
        raise HTTPException(
            status_code=409,
            detail=f"appointment ya está en estado '{appt.status}', no se puede rechazar",
        )

    session_id = await _session_id_de_appointment(appt.lead_id)

    # Caso A: hay fecha alternativa → reagendar (mantiene status='pendiente')
    if body.alternative_datetime:
        try:
            nueva_dt = datetime.fromisoformat(body.alternative_datetime)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"alternative_datetime ISO inválido: {exc}"
            ) from exc

        fields: dict[str, Any] = {"fecha_hora": nueva_dt}
        # Nota de auditoría: que la fecha original quedó en una nota
        nota = (
            appt.notas or ""
        ) + f"\n[Lily reagendó {appt.fecha_hora.isoformat()} → {nueva_dt.isoformat()}]"
        if body.reason:
            nota += f" Motivo: {body.reason}"
        fields["notas"] = nota.strip()

        ok = await update_appointment(appointment_id, fields)
        if not ok:
            raise HTTPException(status_code=500, detail="no se pudo reagendar la cita")

        await emit_event(
            "lead_note_added",
            lead_id=appt.lead_id,
            session_id=session_id,
            description=(
                f"Lily reagendó la cita {appointment_id} "
                f"de {_formato_fecha_humana(appt.fecha_hora)} "
                f"a {_formato_fecha_humana(nueva_dt)}"
            ),
            metadata={
                "appointment_id": appointment_id,
                "from": appt.fecha_hora.isoformat(),
                "to": nueva_dt.isoformat(),
                "reason": body.reason,
            },
        )

        message_sent: dict[str, Any] = {"sent": False, "channel": None}
        if session_id:
            fecha_humana_nueva = _formato_fecha_humana(nueva_dt)
            texto = (
                f"Hola, Lily propone reagendar tu visita para {fecha_humana_nueva}. "
                f"¿Te queda bien ese horario?"
            )
            message_sent = await send_message_to_session(session_id, texto)

        return {
            "success": True,
            "appointment_id": appointment_id,
            "action": "reagendada",
            "fecha_hora": nueva_dt.isoformat(),
            "fecha_hora_anterior": appt.fecha_hora.isoformat(),
            "session_id": session_id,
            "message_sent": message_sent,
        }

    # Caso B: rechazo simple → cancelada
    notas_extra = (appt.notas or "") + f"\n[Lily canceló. Motivo: {body.reason or 'sin motivo'}]"
    ok = await update_appointment(
        appointment_id,
        {"status": "cancelada", "notas": notas_extra.strip()},
    )
    if not ok:
        raise HTTPException(status_code=500, detail="no se pudo cancelar la cita")

    await emit_event(
        "lead_note_added",
        lead_id=appt.lead_id,
        session_id=session_id,
        description=f"Cita {appointment_id} cancelada por Lily",
        metadata={
            "appointment_id": appointment_id,
            "from_status": "pendiente",
            "to_status": "cancelada",
            "reason": body.reason,
        },
    )

    # Si el lead estaba en cita_agendada (improbable porque acaba de cerrarse),
    # podríamos retroceder — pero advance_stage_if_lower no retrocede.
    # Solo limpiamos la nota.
    if session_id:
        lead = await get_lead_by_session(session_id)
        if lead and lead.notes != notas_extra:
            # No tocamos el stage; queda como auditoría en notas
            pass
        if lead:
            await update_lead(
                lead.id,
                {"notes": (lead.notes or "") + f"\n[Cita {appointment_id} rechazada]"},
            )

    message_sent_cancel: dict[str, Any] = {"sent": False, "channel": None}
    if session_id:
        texto = (
            "Hola, no nos fue posible confirmar la fecha que pediste. "
            "¿Podemos buscar otra fecha que te acomode?"
        )
        message_sent_cancel = await send_message_to_session(session_id, texto)

    return {
        "success": True,
        "appointment_id": appointment_id,
        "action": "cancelada",
        "status": "cancelada",
        "reason": body.reason,
        "session_id": session_id,
        "message_sent": message_sent_cancel,
    }
