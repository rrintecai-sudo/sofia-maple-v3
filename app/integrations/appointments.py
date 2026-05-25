"""Helper para CRUD de appointments en Supabase (Bloque C.1 PASO 6).

Schema (creado por Maple Platform):
  id, lead_id, fecha_hora TIMESTAMPTZ, duracion_min, status (enum),
  notas, created_by uuid, created_at

Status enum (appointment_status): pendiente | confirmada | completada |
cancelada | no_show. Sofía SOLO crea en 'pendiente'. Lily aprueba →
'confirmada'. Lily cancela → 'cancelada' + nota con motivo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"pendiente", "confirmada", "completada", "cancelada", "no_show"})


@dataclass
class Appointment:
    id: int
    lead_id: int
    fecha_hora: datetime  # tz-aware (utc según devuelve Supabase)
    duracion_min: int
    status: str
    notas: str | None
    campus_id: int | None = None


def _row_to_appointment(r: dict[str, Any]) -> Appointment:
    ts = r["fecha_hora"]
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    else:
        dt = ts
    return Appointment(
        id=int(r["id"]),
        lead_id=int(r["lead_id"]),
        fecha_hora=dt,
        duracion_min=int(r.get("duracion_min") or 60),
        status=r["status"],
        notas=r.get("notas"),
        campus_id=int(r["campus_id"]) if r.get("campus_id") is not None else None,
    )


def _auth(settings: Settings, *, content_type: bool = False) -> dict[str, str]:
    h = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


async def create_appointment(
    *,
    lead_id: int,
    fecha_hora: datetime,
    duracion_min: int = 60,
    notas: str | None = None,
    campus_id: int | None = None,
    settings: Settings | None = None,
) -> int | None:
    """Inserta cita con status='pendiente'. Devuelve el id o None si falla."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    payload: dict[str, Any] = {
        "lead_id": lead_id,
        "fecha_hora": fecha_hora.isoformat(),
        "duracion_min": duracion_min,
        "status": "pendiente",
    }
    if notas:
        payload["notas"] = notas
    if campus_id is not None:
        payload["campus_id"] = campus_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.supabase_url}/rest/v1/appointments",
                headers={**_auth(settings, content_type=True), "Prefer": "return=representation"},
                json=payload,
            )
    except Exception as exc:
        log.warning("create_appointment httpx_error", extra={"error": str(exc)})
        return None

    if resp.status_code >= 400:
        log.warning(
            "create_appointment http_error",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        return None
    try:
        rows = resp.json()
    except Exception:
        return None
    if rows and isinstance(rows, list):
        return rows[0].get("id")
    return None


async def get_appointment(
    appointment_id: int, *, settings: Settings | None = None
) -> Appointment | None:
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/appointments",
                headers=_auth(settings),
                params={"id": f"eq.{appointment_id}", "select": "*", "limit": "1"},
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_appointment failed", extra={"error": str(exc), "id": appointment_id})
        return None
    return _row_to_appointment(rows[0]) if rows else None


async def update_appointment(
    appointment_id: int,
    fields: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> bool:
    """Actualiza columnas. Si fields incluye `status`, valida contra enum."""
    settings = settings or get_settings()
    if not settings.supabase_url or not fields:
        return False

    payload = dict(fields)
    if "status" in payload and payload["status"] not in VALID_STATUSES:
        log.warning(
            "update_appointment status inválido (se omite)", extra={"status": payload["status"]}
        )
        return False
    if "fecha_hora" in payload and isinstance(payload["fecha_hora"], datetime):
        payload["fecha_hora"] = payload["fecha_hora"].isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{settings.supabase_url}/rest/v1/appointments",
                headers=_auth(settings, content_type=True),
                params={"id": f"eq.{appointment_id}"},
                json=payload,
            )
    except Exception as exc:
        log.warning("update_appointment httpx_error", extra={"error": str(exc)})
        return False

    if resp.status_code >= 400:
        log.warning(
            "update_appointment http_error",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        return False
    return True
