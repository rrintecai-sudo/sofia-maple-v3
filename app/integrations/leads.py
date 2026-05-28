"""Helper para CRUD de leads en Supabase (Bloque C.1 PASO 6).

Sofía y Maple Platform comparten la tabla `leads` (creada por Maple
Platform). Schema clave:
- `parent_name` NOT NULL — Sofía solo crea lead cuando captura nombre
- `channel` NOT NULL — del Canal de Sofía (whatsapp/telegram/web)
- `stage` default 'contacto_inicial', avanza a 'filtro_completado' al
  capturar nombre+nivel+edad, luego 'cita_agendada' al solicitar cita
- `conversation_session_id` — clave para vincular con sofia_conversations

Las funciones son resilientes: ante errores de Supabase, loggean y
devuelven None/False sin levantar (no rompemos el flujo principal).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


# Enums conocidos (espejo de los enums Postgres ya creados por Maple Platform).
VALID_CHANNELS = frozenset(
    {"whatsapp", "telegram", "web", "facebook", "instagram", "directo", "recomendado"}
)
VALID_NIVELES = frozenset({"maternal", "kinder", "primaria", "secundaria", "prepa"})
VALID_STAGES = frozenset(
    {
        "contacto_inicial",
        "filtro_completado",
        "cita_agendada",
        "visita_realizada",
        "papeleria_entregada",
        "proceso_iniciado",
        "descartado",
    }
)


@dataclass
class Lead:
    id: int
    parent_name: str
    parent_phone: str | None
    parent_email: str | None
    child_name: str | None
    child_age: int | None
    child_grade: str | None  # D.3 (Lily 2026-05-27): grado escolar
    nivel: str | None
    channel: str
    stage: str
    source: str
    conversation_session_id: str | None
    notes: str | None


def _row_to_lead(r: dict[str, Any]) -> Lead:
    return Lead(
        id=int(r["id"]),
        parent_name=r["parent_name"],
        parent_phone=r.get("parent_phone"),
        parent_email=r.get("parent_email"),
        child_name=r.get("child_name"),
        child_age=r.get("child_age"),
        child_grade=r.get("child_grade"),
        nivel=r.get("nivel"),
        channel=r["channel"],
        stage=r["stage"],
        source=r["source"],
        conversation_session_id=r.get("conversation_session_id"),
        notes=r.get("notes"),
    )


def _auth_headers(settings: Settings, *, content_type: bool = False) -> dict[str, str]:
    h = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


async def get_lead_by_session(session_id: str, *, settings: Settings | None = None) -> Lead | None:
    """Busca lead por `conversation_session_id`. Devuelve None si no existe
    o si Supabase falla."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/leads",
                headers=_auth_headers(settings),
                params={
                    "conversation_session_id": f"eq.{session_id}",
                    "select": "*",
                    "limit": "1",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning(
            "get_lead_by_session failed", extra={"error": str(exc), "session_id": session_id}
        )
        return None

    return _row_to_lead(rows[0]) if rows else None


async def create_lead(
    *,
    parent_name: str,
    channel: str,
    conversation_session_id: str,
    parent_phone: str | None = None,
    parent_email: str | None = None,
    child_name: str | None = None,
    child_age: int | None = None,
    child_grade: str | None = None,
    nivel: str | None = None,
    notes: str | None = None,
    settings: Settings | None = None,
) -> int | None:
    """Inserta un lead nuevo. Requiere `parent_name` y `channel`.

    Devuelve el id del lead nuevo, o None si falla.
    """
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None
    if channel not in VALID_CHANNELS:
        log.warning("create_lead channel inválido", extra={"channel": channel})
        return None
    if nivel is not None and nivel not in VALID_NIVELES:
        log.warning("create_lead nivel inválido (se omite)", extra={"nivel": nivel})
        nivel = None

    payload: dict[str, Any] = {
        "parent_name": parent_name,
        "channel": channel,
        "conversation_session_id": conversation_session_id,
    }
    for k, v in (
        ("parent_phone", parent_phone),
        ("parent_email", parent_email),
        ("child_name", child_name),
        ("child_age", child_age),
        ("child_grade", child_grade),
        ("nivel", nivel),
        ("notes", notes),
    ):
        if v is not None:
            payload[k] = v

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.supabase_url}/rest/v1/leads",
                headers={
                    **_auth_headers(settings, content_type=True),
                    "Prefer": "return=representation",
                },
                json=payload,
            )
    except Exception as exc:
        log.warning("create_lead httpx_error", extra={"error": str(exc)})
        return None

    if resp.status_code >= 400:
        log.warning(
            "create_lead http_error",
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


async def update_lead(
    lead_id: int,
    fields: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> bool:
    """Actualiza columnas del lead. Filtra enums inválidos por seguridad."""
    settings = settings or get_settings()
    if not settings.supabase_url or not fields:
        return False

    payload = dict(fields)
    if "stage" in payload and payload["stage"] not in VALID_STAGES:
        log.warning("update_lead stage inválido (se omite)", extra={"stage": payload["stage"]})
        payload.pop("stage")
    if "nivel" in payload and payload["nivel"] not in VALID_NIVELES:
        log.warning("update_lead nivel inválido (se omite)", extra={"nivel": payload["nivel"]})
        payload.pop("nivel")
    if "channel" in payload and payload["channel"] not in VALID_CHANNELS:
        payload.pop("channel")
    if not payload:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{settings.supabase_url}/rest/v1/leads",
                headers=_auth_headers(settings, content_type=True),
                params={"id": f"eq.{lead_id}"},
                json=payload,
            )
    except Exception as exc:
        log.warning("update_lead httpx_error", extra={"error": str(exc)})
        return False

    if resp.status_code >= 400:
        log.warning(
            "update_lead http_error", extra={"status": resp.status_code, "body": resp.text[:300]}
        )
        return False
    return True


async def advance_stage_if_lower(
    lead_id: int, current_stage: str, target_stage: str, *, settings: Settings | None = None
) -> bool:
    """Avanza el stage solo si el actual está antes en el pipeline.

    Pipeline order:
      contacto_inicial < filtro_completado < cita_agendada
        < visita_realizada < papeleria_entregada < proceso_iniciado

    `descartado` queda fuera del orden (no se "avanza" desde ahí automáticamente).
    """
    order = [
        "contacto_inicial",
        "filtro_completado",
        "cita_agendada",
        "visita_realizada",
        "papeleria_entregada",
        "proceso_iniciado",
    ]
    if current_stage not in order or target_stage not in order:
        return False
    if order.index(target_stage) <= order.index(current_stage):
        return False
    return await update_lead(lead_id, {"stage": target_stage}, settings=settings)
