"""Webhook de Telegram.

Flujo por mensaje recibido:
1. Parse del Update (texto, voz, imagen, sticker, documento).
2. Normalizar a texto (voz → Whisper, imagen → vision).
3. Push al debouncer con seq_id único.
4. asyncio.sleep(window_seconds).
5. try_claim: si soy el último seq, proceso; si no, abort silencioso.
6. Llamar al orchestrator.procesar_turno con el texto concatenado.
7. Enviar respuesta vía TelegramChannel (texto + typing indicator).

Telegram espera HTTP 200 rápido — el procesamiento corre en background.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.adapters.telegram_client import TelegramChannel, get_telegram
from app.config import get_settings
from app.core.debounce import get_debouncer
from app.core.orchestrator import procesar_turno
from app.core.state import Canal

log = logging.getLogger(__name__)

router = APIRouter(tags=["telegram"])


class TelegramSetupResponse(BaseModel):
    ok: bool
    webhook_url: str
    detail: dict[str, Any] | None = None


@router.post("/webhook/telegram", status_code=status.HTTP_200_OK)
async def webhook_telegram(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
) -> dict[str, str]:
    """Endpoint del webhook de Telegram.

    Responde 200 inmediatamente para no bloquear a Telegram. El trabajo real
    corre en background_tasks.
    """
    settings = get_settings()
    # Validación opcional del secret token
    if settings.telegram_webhook_secret:
        if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            log.warning("telegram webhook with invalid secret")
            raise HTTPException(status_code=403, detail="invalid secret token")

    try:
        update = await request.json()
    except Exception as exc:
        log.warning("telegram webhook with non-json body", extra={"error": str(exc)})
        return {"status": "ignored"}

    background_tasks.add_task(_process_update, update)
    return {"status": "received"}


async def _process_update(update: dict[str, Any]) -> None:
    """Procesa el update en background. Robusto a errores — solo loggea."""
    try:
        await _handle_update(update)
    except Exception as exc:
        log.error(
            "telegram update processing failed",
            extra={"error": str(exc), "update_keys": list(update.keys())},
        )


async def _handle_update(update: dict[str, Any]) -> None:
    """Maneja un Update real."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        log.info("telegram update sin message — ignored", extra={"keys": list(update.keys())})
        return

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        log.warning("telegram message sin chat.id")
        return

    telegram = get_telegram()
    debouncer = get_debouncer()
    session_id = TelegramChannel.session_id_for_chat(chat_id)

    # Normalizar el mensaje a texto
    texto = await _extract_text(msg, telegram)
    if not texto:
        log.info("telegram mensaje sin texto extraíble", extra={"session_id": session_id})
        return

    # Push a debouncer
    seq_id = await debouncer.push_message(session_id, texto)

    # Esperar ventana
    await asyncio.sleep(debouncer.window_seconds)

    # try_claim
    claim = await debouncer.try_claim(session_id, seq_id)
    if not claim.claimed:
        return

    # Procesar turno con typing indicator
    typing_task = asyncio.create_task(_keep_typing(telegram, session_id))
    try:
        result = await procesar_turno(
            mensaje=claim.joined,
            session_id=session_id,
            canal=Canal.TELEGRAM,
        )
    finally:
        typing_task.cancel()

    # Enviar respuesta
    await telegram.send_text(session_id, result.response)
    log.info(
        "telegram turn delivered",
        extra={
            "session_id": session_id,
            "tokens_input": result.tokens_input,
            "tokens_output": result.tokens_output,
            "cost_usd": float(result.cost_usd),
            "latency_ms": result.latency_ms,
        },
    )


async def _extract_text(msg: dict[str, Any], telegram: TelegramChannel) -> str:
    """Devuelve el texto del mensaje, transcribiendo voz o describiendo imagen si aplica."""
    if "text" in msg and isinstance(msg["text"], str):
        return msg["text"].strip()

    if "voice" in msg:
        voice = msg["voice"]
        return await telegram.transcribe_voice({"file_id": voice["file_id"]})

    if "audio" in msg:
        audio = msg["audio"]
        return await telegram.transcribe_voice({"file_id": audio["file_id"]})

    if "photo" in msg:
        # `photo` es lista de tamaños — tomar el más grande
        photos = msg["photo"]
        if photos:
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            descripcion = await telegram.describe_image({"file_id": largest["file_id"]})
            caption = (msg.get("caption") or "").strip()
            if caption:
                return f"{caption}\n\n(imagen adjunta: {descripcion})"
            return f"(imagen adjunta: {descripcion})"

    if "sticker" in msg:
        emoji = msg["sticker"].get("emoji") or "(sticker)"
        return f"(usuario envió un sticker: {emoji})"

    if "document" in msg:
        name = msg["document"].get("file_name", "archivo")
        return f"(usuario envió un documento: {name})"

    return ""


async def _keep_typing(telegram: TelegramChannel, session_id: str) -> None:
    """Re-emite el typing indicator cada 4s mientras Sofía está pensando.

    Telegram limpia el typing tras 5 segundos sin renovar.
    """
    try:
        while True:
            await telegram.typing_indicator(session_id, on=True)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return


# ============================================================
# Endpoints de setup / health (admin only)
# ============================================================


@router.post("/admin/telegram/set-webhook", response_model=TelegramSetupResponse)
async def admin_set_webhook(
    webhook_url: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> TelegramSetupResponse:
    """Configura el webhook de Telegram.

    Llama a Telegram setWebhook. Útil para deploy inicial y rotación de URL.
    """
    settings = get_settings()
    if settings.admin_api_key and x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")

    telegram = get_telegram()
    result = await telegram.set_webhook(
        webhook_url=webhook_url,
        secret_token=settings.telegram_webhook_secret or None,
    )
    return TelegramSetupResponse(ok=bool(result.get("ok")), webhook_url=webhook_url, detail=result)


@router.get("/admin/telegram/webhook-info")
async def admin_get_webhook_info(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> dict[str, Any]:
    settings = get_settings()
    if settings.admin_api_key and x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="invalid admin key")
    info = await get_telegram().get_webhook_info()
    return info
