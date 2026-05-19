"""Tool de envío de imagen.

Para MVP, solo hay UNA imagen disponible: la tabla de costos de Kinder.
La regla está en `prompts/journey/informacion.md` — sólo se envía cuando:
(a) el usuario pidió explícitamente "tabla" o "imagen" Y
(b) el nivel del que se habla es Kinder/Preschool.

Esta tool se llama desde el orchestrator (Bloque 5 wire-up) cuando esas
condiciones se cumplen. El channel se pasa explícitamente para que la misma
tool funcione en Web, Telegram o WhatsApp.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)

# URL de la imagen — placeholder; el técnico anterior la tenía en Google Drive.
# Cuando Cecilia nos pase la versión nueva, se actualiza aquí (o se mueve a tabla).
IMAGEN_COSTOS_KINDER_URL = (
    "https://drive.google.com/uc?id=1j232OKaO8Pjsb0pY2PKQBQd_psW2kprP&export=download"
)


class _SendableChannel(Protocol):
    """Subset del Channel Protocol que send_image necesita."""

    async def send_image(
        self, session_id: str, image_url: str, caption: str | None = None
    ) -> None: ...


async def enviar_imagen_costos_kinder(
    channel: _SendableChannel,
    session_id: str,
    caption: str | None = None,
) -> bool:
    """Envía la imagen de costos Kinder. Retorna True si tuvo éxito."""
    try:
        await channel.send_image(
            session_id=session_id,
            image_url=IMAGEN_COSTOS_KINDER_URL,
            caption=caption,
        )
        log.info("imagen_costos_kinder enviada", extra={"session_id": session_id})
        return True
    except Exception as exc:
        log.error("imagen_costos_kinder falló", extra={"error": str(exc), "session_id": session_id})
        return False
