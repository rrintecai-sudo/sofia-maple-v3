"""Endpoints del canal Web Chat.

- `GET /chat`            sirve la UI mínima (chat.html).
- `POST /webhook/web`    recibe un mensaje del usuario y devuelve la respuesta de Sofía.
- `GET /chat/history/{session_id}` devuelve los últimos N mensajes de la sesión.

NOTA: para MVP usamos request/response simple. SSE (streaming token-a-token) se
puede agregar más adelante; con Haiku 4.5 + caching, la latencia de un turno típico
(~3-5s) es suficiente sin streaming.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.orchestrator import procesar_turno
from app.core.repository import get_repository
from app.core.state import Canal

log = logging.getLogger(__name__)

router = APIRouter(tags=["web-chat"])

WEB_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "web" / "templates"


class WebChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class WebChatResponse(BaseModel):
    session_id: str
    response: str
    turn_number: int
    fase_journey: str
    intent: str | None = None
    tokens_input: int
    tokens_output: int
    tokens_cached: int
    cost_usd: float
    latency_ms: int


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    sofia_web_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Sirve la UI del chat. Asegura cookie de sesión.

    Bug fix crítico: el `response.set_cookie(...)` sobre un parámetro
    `Response` NO se transfiere a un `HTMLResponse(...)` retornado distinto —
    el header `Set-Cookie` nunca llegaba al cliente. Cada POST llegaba sin
    cookie y el endpoint generaba un UUID nuevo por request, dejando cada
    turno como t=0 sin contexto. Fix: setear la cookie sobre el `HTMLResponse`
    que efectivamente se retorna.
    """
    settings = get_settings()
    is_new_session = not sofia_web_session
    if is_new_session:
        sofia_web_session = str(uuid.uuid4())

    html_path = WEB_TEMPLATES / "chat.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="chat.html no encontrado")

    html = html_path.read_text(encoding="utf-8")
    html = html.replace("{{TITLE}}", settings.web_chat_title)
    html = html.replace("{{SESSION_ID}}", f"web:{sofia_web_session}")

    html_response = HTMLResponse(content=html)
    if is_new_session:
        # secure=True solo en prod (HTTPS). httponly y SameSite=Lax protegen
        # contra CSRF/XSS sin romper el flujo same-origin del JS frontend.
        html_response.set_cookie(
            key=settings.web_session_cookie,
            value=sofia_web_session,
            httponly=True,
            secure=settings.is_production,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,  # 30 días
        )
    return html_response


@router.post("/webhook/web", response_model=WebChatResponse)
async def webhook_web(
    body: WebChatRequest,
    request: Request,
    sofia_web_session: str | None = Cookie(default=None),
) -> WebChatResponse:
    """Procesa un mensaje del usuario y devuelve la respuesta de Sofía."""
    if not sofia_web_session:
        # Cliente envió sin cookie — generar fallback de UUID
        sofia_web_session = str(uuid.uuid4())
    session_id = f"web:{sofia_web_session}"

    result = await procesar_turno(
        mensaje=body.content,
        session_id=session_id,
        canal=Canal.WEB,
        tester=False,
    )

    return WebChatResponse(
        session_id=result.session_id,
        response=result.response,
        turn_number=result.turn_number,
        fase_journey=result.fase_journey.value,
        intent=result.intent.value if result.intent else None,
        tokens_input=result.tokens_input,
        tokens_output=result.tokens_output,
        tokens_cached=result.tokens_cached,
        cost_usd=float(result.cost_usd),
        latency_ms=result.latency_ms,
    )


@router.get("/chat/history/{session_id:path}")
async def chat_history(session_id: str, limit: int = 50) -> dict:
    """Devuelve el historial reciente para hidratar la UI al recargar."""
    repo = get_repository()
    rows = await repo.list_recent_messages(session_id, limit=limit)
    return {"session_id": session_id, "messages": rows}
