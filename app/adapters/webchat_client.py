"""Adapter del canal Web Chat — implementa Channel Protocol.

Para web no usamos debounce (los mensajes llegan uno por uno y sincronizados
con la UI). Tampoco soporta voz/imagen en MVP (Bloque 2). Sí soporta streaming
vía SSE en el endpoint dedicado.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class WebChannel:
    """Canal Web Chat. Las respuestas se devuelven al frontend vía HTTP/SSE.

    Este adapter no "envía" mensajes de manera asíncrona al usuario — el flujo
    web es request/response. Los métodos `send_*` quedan como no-op (la respuesta
    se devuelve sincrónicamente desde el endpoint).
    """

    name = "web"

    async def send_text(self, session_id: str, text: str) -> None:
        # En web, la respuesta sale por el endpoint, no por un canal asíncrono.
        # Este método existe para cumplir el Protocol y para uso futuro
        # (notificaciones push, etc.).
        log.debug("WebChannel.send_text (no-op)", extra={"session_id": session_id})

    async def send_image(
        self,
        session_id: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
        log.debug(
            "WebChannel.send_image (no-op, MVP)",
            extra={"session_id": session_id, "image": image_url},
        )

    async def send_sticker(self, session_id: str, sticker_id: str) -> None:
        # En Web, no hay stickers nativos — caption visual sería emoji.
        log.debug("WebChannel.send_sticker (no-op)", extra={"session_id": session_id})

    async def transcribe_voice(self, voice_payload: dict[str, Any]) -> str:
        raise NotImplementedError("Web Chat no soporta voz en MVP (Bloque 2)")

    async def describe_image(self, image_payload: dict[str, Any]) -> str:
        raise NotImplementedError("Web Chat no soporta imagen en MVP (Bloque 2)")

    async def mark_as_read(self, session_id: str, message_id: str) -> None:
        pass  # N/A en web

    async def typing_indicator(self, session_id: str, on: bool = True) -> None:
        pass  # Se maneja en el frontend con la UI; no hay API server-to-client aquí


_singleton: WebChannel | None = None


def get_web_channel() -> WebChannel:
    global _singleton
    if _singleton is None:
        _singleton = WebChannel()
    return _singleton
