"""Adapter de Evolution API (WhatsApp).

Implementa la interfaz `Channel`. Las APIs de Evolution están documentadas en
https://doc.evolution-api.com.

NO conecta a un número WhatsApp real en este bloque — el código queda LISTO
para que en Bloque 6 (deploy WhatsApp QA) sólo se levante una nueva instancia
Evolution y se apunte aquí.

Endpoints usados (relativos a EVOLUTION_BASE_URL):
- POST /message/sendText/{instance}    — texto
- POST /message/sendMedia/{instance}   — imagen
- POST /message/sendSticker/{instance} — sticker
- POST /chat/getBase64FromMediaMessage/{instance} — descargar voz/foto
- PUT  /chat/markMessageAsRead/{instance} — marcar leído
- POST /chat/sendPresence/{instance}   — typing
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from app.adapters.openai_client import get_openai
from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class EvolutionChannel:
    """Implementa Channel para Evolution API (WhatsApp)."""

    name = "whatsapp"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        s = self.settings
        return bool(s.evolution_base_url and s.evolution_instance and s.evolution_api_key)

    @property
    def base_url(self) -> str:
        return self.settings.evolution_base_url.rstrip("/")

    @property
    def instance(self) -> str:
        return self.settings.evolution_instance

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"apikey": self.settings.evolution_api_key},
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------- session_id helpers ----------

    @staticmethod
    def session_id_for_remote(remote_jid: str) -> str:
        """`remote_jid` viene de Evolution como '5218441302112@s.whatsapp.net'."""
        return f"whatsapp:{remote_jid}"

    @staticmethod
    def remote_jid_from_session(session_id: str) -> str:
        canal, _, ident = session_id.partition(":")
        if canal != "whatsapp":
            raise ValueError(f"session_id no es de WhatsApp: {session_id!r}")
        return ident

    @staticmethod
    def number_from_remote_jid(remote_jid: str) -> str:
        """Quita el sufijo '@s.whatsapp.net' para enviar mensajes."""
        return remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")

    # ---------- envío ----------

    async def send_text(self, session_id: str, text: str) -> None:
        remote_jid = self.remote_jid_from_session(session_id)
        number = self.number_from_remote_jid(remote_jid)
        resp = await self.http.post(
            f"/message/sendText/{self.instance}",
            json={"number": number, "text": text},
        )
        if resp.status_code >= 400:
            log.error(
                "evolution sendText failed",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            resp.raise_for_status()

    async def send_image(
        self,
        session_id: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
        remote_jid = self.remote_jid_from_session(session_id)
        number = self.number_from_remote_jid(remote_jid)
        payload: dict[str, Any] = {
            "number": number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "media": image_url,
        }
        if caption:
            payload["caption"] = caption
        resp = await self.http.post(f"/message/sendMedia/{self.instance}", json=payload)
        resp.raise_for_status()

    async def send_sticker(self, session_id: str, sticker_id: str) -> None:
        """`sticker_id` debe ser URL pública de .webp."""
        remote_jid = self.remote_jid_from_session(session_id)
        number = self.number_from_remote_jid(remote_jid)
        resp = await self.http.post(
            f"/message/sendSticker/{self.instance}",
            json={"number": number, "sticker": sticker_id},
        )
        resp.raise_for_status()

    async def mark_as_read(self, session_id: str, message_id: str) -> None:
        """Marca el mensaje como leído. `message_id` viene del Webhook de Evolution."""
        remote_jid = self.remote_jid_from_session(session_id)
        try:
            await self.http.post(
                f"/chat/markMessageAsRead/{self.instance}",
                json={"remoteJid": remote_jid, "messageId": message_id},
            )
        except Exception as exc:
            log.debug("evolution mark_as_read failed", extra={"error": str(exc)})

    async def typing_indicator(self, session_id: str, on: bool = True) -> None:
        remote_jid = self.remote_jid_from_session(session_id)
        number = self.number_from_remote_jid(remote_jid)
        try:
            await self.http.post(
                f"/chat/sendPresence/{self.instance}",
                json={
                    "number": number,
                    "presence": "composing" if on else "paused",
                    "delay": 1000,
                },
            )
        except Exception as exc:
            log.debug("evolution typing failed", extra={"error": str(exc)})

    # ---------- audio/voz ----------

    async def transcribe_voice(self, voice_payload: dict[str, Any]) -> str:
        """Espera dict con `message_id` o el base64 directamente como `audio_base64`.

        Evolution puede pasar el audio inline en el webhook (base64) o por message_id.
        """
        audio_b64 = voice_payload.get("audio_base64")
        if not audio_b64 and voice_payload.get("message_id"):
            audio_b64 = await self._download_media_base64(voice_payload["message_id"])
        if not audio_b64:
            raise ValueError("voice_payload sin audio_base64 ni message_id")

        audio_bytes = base64.b64decode(audio_b64)
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

    # ---------- imagen ----------

    async def describe_image(self, image_payload: dict[str, Any]) -> str:
        img_b64 = image_payload.get("image_base64")
        if not img_b64 and image_payload.get("message_id"):
            img_b64 = await self._download_media_base64(image_payload["message_id"])
        if not img_b64:
            raise ValueError("image_payload sin image_base64 ni message_id")

        data_url = f"data:image/jpeg;base64,{img_b64}"
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

    async def _download_media_base64(self, message_id: str) -> str:
        """Llama a Evolution para obtener el media en base64."""
        resp = await self.http.post(
            f"/chat/getBase64FromMediaMessage/{self.instance}",
            json={"message": {"key": {"id": message_id}}},
        )
        resp.raise_for_status()
        return resp.json().get("base64", "")

    # ---------- health ----------

    async def health_check(self) -> dict[str, Any]:
        if not self.is_configured():
            return {"status": "skip", "detail": "no evolution config"}
        try:
            resp = await self.http.get(f"/instance/connectionState/{self.instance}")
            if resp.status_code == 200:
                state = resp.json().get("instance", {}).get("state", "unknown")
                if state == "open":
                    return {"status": "ok", "detail": state}
                return {"status": "unreachable", "detail": f"instance state={state}"}
            return {"status": "unauthorized", "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: EvolutionChannel | None = None


def get_evolution() -> EvolutionChannel:
    global _singleton
    if _singleton is None:
        _singleton = EvolutionChannel()
    return _singleton
