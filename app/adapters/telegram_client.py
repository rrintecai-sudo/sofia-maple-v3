"""Adapter de Telegram Bot API.

Implementa la interfaz `Channel`. Llama directo a la HTTP API de Telegram con
httpx (más simple y transparente que python-telegram-bot para nuestro caso).

Soporta:
- send_text (sendMessage con Markdown)
- send_image (sendPhoto desde URL o file_id)
- send_sticker (sendSticker)
- transcribe_voice (descarga .ogg + Whisper)
- describe_image (descarga photo + gpt-4o-mini vision)
- mark_as_read (no-op — Telegram lo maneja automáticamente al responder)
- typing_indicator (sendChatAction)
- set_webhook (configuración inicial)
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from app.adapters.openai_client import get_openai
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramChannel:
    """Implementa Channel para Telegram Bot API."""

    name = "telegram"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def token(self) -> str:
        if not self.settings.telegram_bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN no configurado. Genéralo con @BotFather y agrégalo al .env."
            )
        return self.settings.telegram_bot_token

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{TELEGRAM_API_BASE}/bot{self.token}",
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    # ---------- session_id helpers ----------

    @staticmethod
    def session_id_for_chat(chat_id: int | str) -> str:
        return f"telegram:{chat_id}"

    @staticmethod
    def chat_id_from_session(session_id: str) -> int:
        canal, _, ident = session_id.partition(":")
        if canal != "telegram":
            raise ValueError(f"session_id no es de Telegram: {session_id!r}")
        return int(ident)

    # ---------- envío ----------

    async def send_text(self, session_id: str, text: str) -> None:
        chat_id = self.chat_id_from_session(session_id)
        # Convertir negritas tipo WhatsApp (*texto*) a Markdown V2 de Telegram
        # Para simplificar usamos Markdown (no V2) que permite *bold* nativo.
        resp = await self.http.post(
            "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            },
        )
        if resp.status_code >= 400:
            # Si falla por parsing markdown, reintenta sin parse_mode
            log.warning(
                "telegram sendMessage markdown failed, retrying plain",
                extra={"status": resp.status_code},
            )
            resp = await self.http.post("/sendMessage", json={"chat_id": chat_id, "text": text})
            resp.raise_for_status()

    async def send_image(
        self,
        session_id: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
        chat_id = self.chat_id_from_session(session_id)
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": image_url}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "Markdown"
        resp = await self.http.post("/sendPhoto", json=payload)
        resp.raise_for_status()

    async def send_sticker(self, session_id: str, sticker_id: str) -> None:
        """`sticker_id` puede ser un file_id de Telegram o URL pública de un .webp."""
        chat_id = self.chat_id_from_session(session_id)
        resp = await self.http.post(
            "/sendSticker", json={"chat_id": chat_id, "sticker": sticker_id}
        )
        resp.raise_for_status()

    async def mark_as_read(self, session_id: str, message_id: str) -> None:
        # Telegram no expone una API de "marcar leído" para bots
        # (se marca automático cuando el bot responde). No-op.
        return None

    async def typing_indicator(self, session_id: str, on: bool = True) -> None:
        if not on:
            return None  # Telegram limpia el typing solo tras 5s sin renovar
        chat_id = self.chat_id_from_session(session_id)
        try:
            await self.http.post(
                "/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
        except Exception as exc:
            log.warning("telegram typing_indicator failed", extra={"error": str(exc)})

    # ---------- transcripción y vision ----------

    async def transcribe_voice(self, voice_payload: dict[str, Any]) -> str:
        """Espera dict con 'file_id'. Descarga el .ogg y lo manda a Whisper."""
        file_id = voice_payload.get("file_id")
        if not file_id:
            raise ValueError("voice_payload sin file_id")
        audio_bytes = await self._download_telegram_file(file_id)
        # Whisper espera multipart con un archivo
        openai = get_openai()
        files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai.settings.openai_api_key}"},
                files=files,
                data={"model": "whisper-1", "language": "es"},
            )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()

    async def describe_image(self, image_payload: dict[str, Any]) -> str:
        """Espera dict con 'file_id'. Descarga la foto y la analiza con gpt-4o-mini vision."""
        file_id = image_payload.get("file_id")
        if not file_id:
            raise ValueError("image_payload sin file_id")
        img_bytes = await self._download_telegram_file(file_id)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"

        openai = get_openai()
        completion = await openai.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe qué ves en esta imagen en 1-2 oraciones en español.",
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=200,
        )
        return (completion.choices[0].message.content or "").strip()

    async def _download_telegram_file(self, file_id: str) -> bytes:
        """getFile + descarga binaria desde el CDN de Telegram."""
        meta_resp = await self.http.get("/getFile", params={"file_id": file_id})
        meta_resp.raise_for_status()
        file_path = meta_resp.json()["result"]["file_path"]
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(f"{TELEGRAM_API_BASE}/file/bot{self.token}/{file_path}")
            resp.raise_for_status()
            return resp.content

    # ---------- configuración de webhook ----------

    async def set_webhook(
        self, webhook_url: str, secret_token: str | None = None
    ) -> dict[str, Any]:
        """Configura el webhook de Telegram apuntando a `webhook_url`.

        `secret_token`: opcional, si lo configuras Telegram envía un header
        `X-Telegram-Bot-Api-Secret-Token` que puedes validar.
        """
        payload: dict[str, Any] = {
            "url": webhook_url,
            "drop_pending_updates": True,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if secret_token:
            payload["secret_token"] = secret_token
        resp = await self.http.post("/setWebhook", json=payload)
        resp.raise_for_status()
        result = resp.json()
        log.info("telegram webhook set", extra={"url": webhook_url, "result": result})
        return result

    async def get_webhook_info(self) -> dict[str, Any]:
        resp = await self.http.get("/getWebhookInfo")
        resp.raise_for_status()
        return resp.json().get("result", {})

    async def delete_webhook(self) -> None:
        resp = await self.http.post("/deleteWebhook", json={"drop_pending_updates": True})
        resp.raise_for_status()

    async def health_check(self) -> dict[str, Any]:
        """Verifica que el token es válido vía getMe."""
        if not self.is_configured():
            return {"status": "skip", "detail": "no telegram token"}
        try:
            resp = await self.http.get("/getMe")
            if resp.status_code == 200 and resp.json().get("ok"):
                return {"status": "ok", "detail": resp.json()["result"].get("username")}
            return {"status": "unauthorized", "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: TelegramChannel | None = None


def get_telegram() -> TelegramChannel:
    global _singleton
    if _singleton is None:
        _singleton = TelegramChannel()
    return _singleton
