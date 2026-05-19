"""Modo Aprendizaje seguro — registra feedback del equipo SIN aplicar cambios.

Cuando el equipo (Lily, Gaby, Cecilia) escribe `maple2026`, Sofía entra en
"Modo Aprendizaje". Los mensajes siguientes se guardan en
`sofia_feedback_pending` con estado `pending`. NUNCA se aplican automáticamente
al prompt — un humano (Oscar) los revisa y crea un PR si aplican.

Ver ARCHITECTURE §9.2 y SOFIA_BUILD_PLAN Bloque 4 Paso 5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


CATEGORIAS_FEEDBACK = (
    "tono",
    "precio",
    "objecion",
    "proceso",
    "informacion",
    "prohibicion",
    "otro",
)


@dataclass(frozen=True)
class FeedbackPending:
    id: int
    session_id: str
    feedback_text: str
    contexto_anterior: str | None
    propuesta_cambio: str | None
    categoria: str | None
    estado: str
    created_at: str
    revised_by: str | None
    revised_at: str | None
    pr_url: str | None
    notas_revision: str | None


def _supa_headers(settings: Settings) -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/json",
    }


async def guardar_feedback(
    session_id: str,
    feedback_text: str,
    *,
    contexto_anterior: str | None = None,
    propuesta_cambio: str | None = None,
    categoria: str | None = None,
    settings: Settings | None = None,
) -> int | None:
    """Inserta una fila en `sofia_feedback_pending` con estado=pending.

    Retorna el id insertado o None si falla. NO levanta excepción —
    es resiliente para que el flujo del chat no se rompa.
    """
    settings = settings or get_settings()
    if not settings.supabase_url:
        log.warning("guardar_feedback: supabase no configurado")
        return None

    payload: dict[str, Any] = {
        "session_id": session_id,
        "feedback_text": feedback_text,
        "estado": "pending",
    }
    if contexto_anterior:
        payload["contexto_anterior"] = contexto_anterior
    if propuesta_cambio:
        payload["propuesta_cambio"] = propuesta_cambio
    if categoria and categoria in CATEGORIAS_FEEDBACK:
        payload["categoria"] = categoria

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.supabase_url}/rest/v1/sofia_feedback_pending",
                headers={**_supa_headers(settings), "Prefer": "return=representation"},
                json=payload,
            )
        if resp.status_code >= 400:
            log.warning(
                "guardar_feedback failed",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return None
        rows = resp.json()
        return int(rows[0]["id"]) if rows else None
    except Exception as exc:
        log.warning("guardar_feedback exception", extra={"error": str(exc)})
        return None


async def listar_feedback_pendiente(
    *,
    limit: int = 50,
    settings: Settings | None = None,
) -> list[FeedbackPending]:
    """Lista feedback con estado=pending, ordenado por más reciente."""
    settings = settings or get_settings()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/sofia_feedback_pending",
                headers=_supa_headers(settings),
                params={
                    "estado": "eq.pending",
                    "select": "*",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
            )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("listar_feedback_pendiente failed", extra={"error": str(exc)})
        return []

    return [_row_to_feedback(r) for r in resp.json()]


async def revisar_feedback(
    feedback_id: int,
    decision: Literal["approved", "rejected", "merged"],
    *,
    revised_by: str | None = None,
    pr_url: str | None = None,
    notas: str | None = None,
    settings: Settings | None = None,
) -> bool:
    """Actualiza el estado de un feedback. Retorna True si tuvo éxito."""
    settings = settings or get_settings()
    payload: dict[str, Any] = {
        "estado": decision,
        "revised_by": revised_by,
        "revised_at": "now()",
    }
    if pr_url:
        payload["pr_url"] = pr_url
    if notas:
        payload["notas_revision"] = notas

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"{settings.supabase_url}/rest/v1/sofia_feedback_pending",
                headers=_supa_headers(settings),
                params={"id": f"eq.{feedback_id}"},
                json=payload,
            )
        return resp.status_code < 400
    except Exception as exc:
        log.warning("revisar_feedback failed", extra={"error": str(exc)})
        return False


def _row_to_feedback(row: dict[str, Any]) -> FeedbackPending:
    return FeedbackPending(
        id=int(row["id"]),
        session_id=row["session_id"],
        feedback_text=row["feedback_text"],
        contexto_anterior=row.get("contexto_anterior"),
        propuesta_cambio=row.get("propuesta_cambio"),
        categoria=row.get("categoria"),
        estado=row.get("estado", "pending"),
        created_at=row.get("created_at", ""),
        revised_by=row.get("revised_by"),
        revised_at=row.get("revised_at"),
        pr_url=row.get("pr_url"),
        notas_revision=row.get("notas_revision"),
    )
