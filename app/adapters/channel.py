"""Interfaz unificada de canal.

El orchestrator es agnóstico al canal. Cada implementación (Evolution, Telegram,
WebChat) cumple esta interfaz. Ver ARCHITECTURE sección 9.5.
"""

from __future__ import annotations

from typing import Any, Protocol


class Channel(Protocol):
    """Protocolo común para WhatsApp / Telegram / Web Chat.

    Las implementaciones concretas viven en `app/adapters/{evolution,telegram,webchat}_client.py`
    y se construirán en Bloques 2-5.
    """

    name: str  # 'whatsapp' | 'telegram' | 'web'

    async def send_text(self, session_id: str, text: str) -> None:
        """Envía un mensaje de texto al usuario."""
        ...

    async def send_image(
        self,
        session_id: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
        """Envía una imagen con caption opcional."""
        ...

    async def send_sticker(self, session_id: str, sticker_id: str) -> None:
        """Envía un sticker. WhatsApp/Telegram tienen formatos diferentes."""
        ...

    async def transcribe_voice(self, voice_payload: dict[str, Any]) -> str:
        """Transcribe audio a texto. Cada canal entrega el payload distinto."""
        ...

    async def describe_image(self, image_payload: dict[str, Any]) -> str:
        """Describe una imagen recibida (vision)."""
        ...

    async def mark_as_read(self, session_id: str, message_id: str) -> None:
        """Marca el mensaje como leído (WhatsApp/Telegram). No-op en Web."""
        ...

    async def typing_indicator(self, session_id: str, on: bool = True) -> None:
        """Activa/desactiva el indicador 'escribiendo...'."""
        ...


def parse_session_id(session_id: str) -> tuple[str, str]:
    """Extrae (canal, identificador) de un session_id prefijado.

    Ejemplos:
        'whatsapp:5218441302112@s.whatsapp.net' → ('whatsapp', '5218441302112@s.whatsapp.net')
        'telegram:123456789'                    → ('telegram', '123456789')
        'web:abc-uuid'                          → ('web', 'abc-uuid')
    """
    canal, _, identificador = session_id.partition(":")
    if not canal or not identificador:
        raise ValueError(f"session_id inválido (sin prefijo de canal): {session_id!r}")
    if canal not in ("whatsapp", "telegram", "web"):
        raise ValueError(f"canal desconocido: {canal!r}")
    return canal, identificador
