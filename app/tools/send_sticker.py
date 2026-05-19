"""Tool de envío de sticker (despedida cálida)."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)

# Sticker de despedida. WhatsApp acepta URLs de .webp; Telegram acepta file_id o URL.
# Placeholder: usamos el sticker que tenía la Sofia anterior en Drive.
STICKER_DESPEDIDA_URL = (
    "https://drive.google.com/uc?id=1H26mUUubNVGorP76Pz5mwi65Qs4RSms9&export=download"
)


class _SendableChannel(Protocol):
    async def send_sticker(self, session_id: str, sticker_id: str) -> None: ...


async def enviar_sticker_despedida(channel: _SendableChannel, session_id: str) -> bool:
    """Envía el sticker de despedida. Retorna True si éxito."""
    try:
        await channel.send_sticker(session_id=session_id, sticker_id=STICKER_DESPEDIDA_URL)
        log.info("sticker_despedida enviado", extra={"session_id": session_id})
        return True
    except Exception as exc:
        log.error(
            "sticker_despedida falló",
            extra={"error": str(exc), "session_id": session_id},
        )
        return False
