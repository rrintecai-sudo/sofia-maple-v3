"""Admin endpoints internos. Protegidos con X-Admin-Key.

Bloque 4: feedback del Modo Aprendizaje.
Bloque 5: conversations, turn-logs, stats, costs, replay + UI HTML mínima con Jinja2.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import get_settings
from app.core.learning_mode import (
    FeedbackPending,
    listar_feedback_pendiente,
    revisar_feedback,
)
from app.core.orchestrator import procesar_turno
from app.core.repository import get_repository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "admin"
_templates: Jinja2Templates | None = None


def _get_templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    return _templates


def _check_admin(x_admin_key: str | None) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        # Si no hay admin key configurada, permitir (modo dev). En prod siempre debería estar.
        return
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")


def _check_admin_via_query_or_header(key_query: str | None, key_header: str | None) -> str | None:
    """Para los endpoints HTML, permite pasar la key como query param ?k=...
    (porque los navegadores no envían headers custom en GET de URL pegada).

    Retorna la key efectiva si pasa.
    """
    settings = get_settings()
    if not settings.admin_api_key:
        return None
    effective = key_query or key_header
    if effective != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")
    return effective


# ============================================================
# Modo Aprendizaje (Bloque 4)
# ============================================================


class FeedbackOut(BaseModel):
    id: int
    session_id: str
    feedback_text: str
    contexto_anterior: str | None
    categoria: str | None
    estado: str
    created_at: str


class FeedbackReviewIn(BaseModel):
    decision: Literal["approved", "rejected", "merged"]
    revised_by: str | None = None
    pr_url: str | None = None
    notas: str | None = None


@router.get("/feedback/pending", response_model=list[FeedbackOut])
async def listar_feedback(
    limit: int = Query(default=50, ge=1, le=200),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> list[FeedbackOut]:
    _check_admin(x_admin_key)
    items = await listar_feedback_pendiente(limit=limit)
    return [_to_feedback_out(i) for i in items]


@router.post("/feedback/{feedback_id}/review")
async def review_feedback(
    feedback_id: int,
    body: FeedbackReviewIn,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict:
    _check_admin(x_admin_key)
    ok = await revisar_feedback(
        feedback_id=feedback_id,
        decision=body.decision,
        revised_by=body.revised_by,
        pr_url=body.pr_url,
        notas=body.notas,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="revisión falló")
    return {"ok": True, "id": feedback_id, "decision": body.decision}


def _to_feedback_out(f: FeedbackPending) -> FeedbackOut:
    return FeedbackOut(
        id=f.id,
        session_id=f.session_id,
        feedback_text=f.feedback_text,
        contexto_anterior=f.contexto_anterior,
        categoria=f.categoria,
        estado=f.estado,
        created_at=f.created_at,
    )


# ============================================================
# Conversaciones y turn-logs (Bloque 5)
# ============================================================


@router.get("/conversations")
async def admin_list_conversations(
    canal: str | None = Query(default=None, description="whatsapp | telegram | web"),
    since: str | None = Query(default=None, description="ISO date, ej. 2026-05-15"),
    limit: int = Query(default=50, ge=1, le=500),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> list[dict[str, Any]]:
    _check_admin(x_admin_key)
    repo = get_repository()
    return await repo.list_conversations(canal=canal, since=since, limit=limit)


@router.get("/conversations/{session_id:path}")
async def admin_get_conversation(
    session_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    _check_admin(x_admin_key)
    repo = get_repository()
    conv = await repo.get_conversation_raw(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = await repo.list_all_messages(session_id)
    return {"conversation": conv, "messages": messages}


@router.get("/turn-logs/{session_id:path}")
async def admin_turn_logs(
    session_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> list[dict[str, Any]]:
    _check_admin(x_admin_key)
    repo = get_repository()
    return await repo.list_turn_logs(session_id)


# ============================================================
# Stats y costos (Bloque 5)
# ============================================================


@router.get("/stats")
async def admin_stats(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    _check_admin(x_admin_key)
    repo = get_repository()
    stats = await repo.stats(from_date=from_date, to_date=to_date)
    stats["top_expensive_conversations"] = await repo.top_expensive_conversations(limit=10)
    return stats


@router.get("/costs")
async def admin_costs(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    """Desglose de costos por modelo y por sesión (top 20)."""
    _check_admin(x_admin_key)
    repo = get_repository()
    stats = await repo.stats(from_date=from_date, to_date=to_date)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "total_cost_usd": stats["total_cost_usd"],
        "avg_cost_per_turn_usd": stats["avg_cost_per_turn_usd"],
        "total_tokens_input": stats["total_tokens_input"],
        "total_tokens_output": stats["total_tokens_output"],
        "total_tokens_cached": stats["total_tokens_cached"],
        "cache_ratio": stats["cache_ratio"],
        "models_used": stats["models_used"],
        "top_expensive_conversations": await repo.top_expensive_conversations(limit=20),
    }


# ============================================================
# Replay (re-correr un mensaje contra el prompt/validators actuales)
# ============================================================


class ReplayIn(BaseModel):
    mensaje: str
    session_id: str | None = None  # si no, usa uno temporal "web:replay-<random>"


@router.post("/replay")
async def admin_replay(
    body: ReplayIn,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    """Re-corre un mensaje contra el prompt + validators actuales.

    Útil para probar cambios en el prompt sin involucrar un usuario real. Si no
    pasas session_id, se usa uno aislado ("web:replay-...") para no contaminar
    sesiones reales.
    """
    _check_admin(x_admin_key)
    import uuid

    session_id = body.session_id or f"web:replay-{uuid.uuid4().hex[:8]}"
    from app.core.state import Canal

    canal_str, _, _ = session_id.partition(":")
    try:
        canal = Canal(canal_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"session_id inválido: {session_id}") from exc

    result = await procesar_turno(
        mensaje=body.mensaje,
        session_id=session_id,
        canal=canal,
        tester=True,
    )
    return {
        "session_id": result.session_id,
        "response": result.response,
        "intent": result.intent.value if result.intent else None,
        "fase": result.fase_journey.value,
        "tokens_input": result.tokens_input,
        "tokens_output": result.tokens_output,
        "tokens_cached": result.tokens_cached,
        "cost_usd": float(result.cost_usd),
        "latency_ms": result.latency_ms,
        "turn_number": result.turn_number,
    }


# ============================================================
# UI HTML del Admin Dashboard
# ============================================================


@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    k: str | None = Query(default=None, description="admin key (acepta ?k= o header)"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> HTMLResponse:
    """Dashboard principal. URL: /admin/?k=<admin_key>"""
    _check_admin_via_query_or_header(k, x_admin_key)
    repo = get_repository()
    stats = await repo.stats()
    stats["top_expensive_conversations"] = await repo.top_expensive_conversations(limit=10)
    conversations = await repo.list_conversations(limit=30)
    feedback = await listar_feedback_pendiente(limit=10)
    templates = _get_templates()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stats": stats,
            "conversations": conversations,
            "feedback": feedback,
            "admin_key": k or "",
        },
    )


@router.get("/conv/{session_id:path}", response_class=HTMLResponse)
async def admin_conv_detail(
    request: Request,
    session_id: str,
    k: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> HTMLResponse:
    """Detalle de una conversación específica."""
    _check_admin_via_query_or_header(k, x_admin_key)
    repo = get_repository()
    conv = await repo.get_conversation_raw(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = await repo.list_all_messages(session_id)
    turn_logs = await repo.list_turn_logs(session_id)
    # Indexa turn_logs por turn_number para alinearlos con mensajes assistant
    logs_by_turn = {log["turn_number"]: log for log in turn_logs}
    templates = _get_templates()
    return templates.TemplateResponse(
        request,
        "conversation.html",
        {
            "conv": conv,
            "messages": messages,
            "turn_logs": turn_logs,
            "logs_by_turn": logs_by_turn,
            "admin_key": k or "",
        },
    )
