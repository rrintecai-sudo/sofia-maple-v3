"""Dispatcher por canal — envía un mensaje proactivo a una sesión.

Usado por los endpoints `/api/appointments/{id}/approve|reject` (Bloque
C.1 PASO 7) para notificar al papá por el mismo canal donde conversó.

Web Chat NO soporta push proactivo (HTML request/response): para web,
el mensaje se persiste vía repository.insert_message como un assistant
message — el papá lo verá la próxima vez que abra la conversación.
"""

from __future__ import annotations

import logging
from typing import Any

from app.adapters.channel import parse_session_id

log = logging.getLogger(__name__)


async def send_message_to_session(session_id: str, text: str) -> dict[str, Any]:
    """Envía un mensaje al papá por el canal correcto.

    Returns:
        dict con `sent: bool`, `channel: str`, `detail: str|None`.
    """
    try:
        canal, _identificador = parse_session_id(session_id)
    except ValueError as exc:
        log.warning(
            "dispatcher session_id_invalido", extra={"session_id": session_id, "error": str(exc)}
        )
        return {"sent": False, "channel": "unknown", "detail": "session_id inválido"}

    if canal == "telegram":
        from app.adapters.telegram_client import get_telegram

        try:
            await get_telegram().send_text(session_id, text)
            return {"sent": True, "channel": "telegram", "detail": None}
        except Exception as exc:
            log.warning("dispatcher telegram error", extra={"error": str(exc)})
            return {"sent": False, "channel": "telegram", "detail": str(exc)[:200]}

    if canal == "whatsapp":
        from app.adapters.evolution_client import get_evolution

        try:
            await get_evolution().send_text(session_id, text)
            return {"sent": True, "channel": "whatsapp", "detail": None}
        except Exception as exc:
            log.warning("dispatcher whatsapp error", extra={"error": str(exc)})
            return {"sent": False, "channel": "whatsapp", "detail": str(exc)[:200]}

    if canal == "web":
        # Web no tiene push. Persistimos como assistant message para que el
        # papá lo vea la próxima vez que abra la conversación.
        from app.core.repository import get_repository

        try:
            await get_repository().insert_message(
                session_id=session_id, role="assistant", content=text
            )
            return {
                "sent": True,
                "channel": "web",
                "detail": "persisted_for_next_visit",
            }
        except Exception as exc:
            log.warning("dispatcher web error", extra={"error": str(exc)})
            return {"sent": False, "channel": "web", "detail": str(exc)[:200]}

    return {"sent": False, "channel": canal, "detail": "canal no soportado"}
