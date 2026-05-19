"""Admin endpoints internos. Protegidos con X-Admin-Key.

Para Bloque 4 sólo incluye:
- /admin/feedback/pending — lista de feedback en Modo Aprendizaje
- /admin/feedback/{id}/approve — marca aprobado (NO aplica al prompt)
- /admin/feedback/{id}/reject — archiva

El resto de admin (conversations, stats, costs, replay) viene en Bloque 5.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.core.learning_mode import (
    FeedbackPending,
    listar_feedback_pendiente,
    revisar_feedback,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_admin(x_admin_key: str | None) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        # Si no hay admin key configurada, permitir (modo dev). En prod siempre debería estar.
        return
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")


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
    return [_to_out(i) for i in items]


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


def _to_out(f: FeedbackPending) -> FeedbackOut:
    return FeedbackOut(
        id=f.id,
        session_id=f.session_id,
        feedback_text=f.feedback_text,
        contexto_anterior=f.contexto_anterior,
        categoria=f.categoria,
        estado=f.estado,
        created_at=f.created_at,
    )
