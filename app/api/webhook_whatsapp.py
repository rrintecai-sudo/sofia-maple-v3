"""Webhook de WhatsApp vía Evolution API (Bloque WhatsApp — 28-may-2026).

Recibe POSTs de Evolution con eventos `MESSAGES_UPSERT` y procesa el mensaje
por el mismo `procesar_turno()` del orchestrator que usan los otros canales.

Diseño:
- Ignora mensajes propios (fromMe=true) → evita eco infinito.
- Ignora la instancia equivocada → seguridad contra apuntar webhook a la
  instancia v1 por error.
- Ignora grupos (`@g.us`) → este flujo es 1-a-1 prospecto/Sofía.
- Normaliza texto / audio (Whisper) / imagen (gpt-4o-mini vision).
- Usa el mismo debouncer (7s) que los otros canales para agrupar mensajes
  consecutivos del papá.
- Responde por Evolution.send_text al mismo número.

Evolution responde 200 rápido — el procesamiento corre en background_tasks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, status

from app.adapters.evolution_client import EvolutionChannel, get_evolution
from app.config import get_settings
from app.core.debounce import get_debouncer
from app.core.orchestrator import procesar_turno
from app.core.state import Canal

log = logging.getLogger(__name__)

router = APIRouter(tags=["whatsapp"])


# ============================================================
# Endpoint público
# ============================================================


@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
async def webhook_whatsapp(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Endpoint del webhook de Evolution. Responde 200 inmediatamente."""
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("whatsapp webhook with non-json body", extra={"error": str(exc)})
        return {"status": "ignored"}

    background_tasks.add_task(_process_event, payload)
    return {"status": "received"}


# ============================================================
# Procesamiento en background
# ============================================================


async def _process_event(payload: dict[str, Any]) -> None:
    """Maneja un evento de Evolution. Robusto a errores — solo loggea."""
    try:
        await _handle_event(payload)
    except Exception as exc:
        log.error(
            "whatsapp event processing failed",
            extra={
                "error": str(exc),
                "event": payload.get("event"),
                "instance": payload.get("instance"),
            },
        )


async def _handle_event(payload: dict[str, Any]) -> None:
    settings = get_settings()
    expected_instance = settings.evolution_instance

    event = (payload.get("event") or "").lower()
    instance = payload.get("instance")

    # Sólo procesamos messages.upsert; ignoramos QR updates, connection updates, etc.
    if event not in ("messages.upsert", "messages_upsert"):
        log.debug(
            "whatsapp ignored non-message event",
            extra={"event": event, "instance": instance},
        )
        return

    # 🔒 Hard guard: la instancia que envía DEBE ser la nuestra. Si Evolution
    # apuntara mal el webhook (o si el endpoint quedara expuesto), NO
    # procesamos eventos de otras instancias. La de producción v1 es
    # 'Maple Sofia'; nosotros sólo aceptamos la configurada en settings.
    if expected_instance and instance != expected_instance:
        log.warning(
            "whatsapp event from unexpected instance — ignored",
            extra={"got": instance, "expected": expected_instance},
        )
        return

    data = payload.get("data") or {}
    key = data.get("key") or {}

    # Ignora ecos de Sofía misma
    if key.get("fromMe") is True:
        return

    remote_jid: str = key.get("remoteJid") or ""
    if not remote_jid:
        log.warning("whatsapp event sin remoteJid")
        return

    # Ignora grupos y broadcasts (sólo conversaciones 1-a-1 con prospectos)
    if remote_jid.endswith("@g.us") or remote_jid.endswith("@broadcast"):
        log.info("whatsapp ignored group/broadcast", extra={"remote_jid": remote_jid})
        return

    evolution = get_evolution()
    debouncer = get_debouncer()
    session_id = EvolutionChannel.session_id_for_remote(remote_jid)

    # Normalizar el mensaje a texto
    texto = await _extract_text(data, evolution)
    if not texto:
        log.info("whatsapp mensaje sin texto extraíble", extra={"session_id": session_id})
        return

    # Marcar como leído (best-effort)
    message_id = key.get("id")
    if message_id:
        await evolution.mark_as_read(session_id, message_id)

    # Push a debouncer
    seq_id = await debouncer.push_message(session_id, texto)

    # Esperar ventana
    await asyncio.sleep(debouncer.window_seconds)

    # try_claim — sólo el último seq_id procesa el turno
    claim = await debouncer.try_claim(session_id, seq_id)
    if not claim.claimed:
        return

    # Procesar turno con typing indicator
    typing_task = asyncio.create_task(_keep_typing(evolution, session_id))
    try:
        result = await procesar_turno(
            mensaje=claim.joined,
            session_id=session_id,
            canal=Canal.WHATSAPP,
        )
    finally:
        typing_task.cancel()

    # Enviar respuesta
    await evolution.send_text(session_id, result.response)
    log.info(
        "whatsapp turn delivered",
        extra={
            "session_id": session_id,
            "tokens_input": result.tokens_input,
            "tokens_output": result.tokens_output,
            "cost_usd": float(result.cost_usd),
            "latency_ms": result.latency_ms,
        },
    )


# ============================================================
# Extracción de texto / audio / imagen
# ============================================================


async def _extract_text(data: dict[str, Any], evolution: EvolutionChannel) -> str:
    """Devuelve el texto del mensaje. Transcribe voz, describe imagen.

    Evolution puede mandar el media en base64 inline (config base64=true en el
    webhook). Si no, el adapter lo descarga con /chat/getBase64FromMediaMessage
    usando el message_id.
    """
    msg = data.get("message") or {}
    message_type = (data.get("messageType") or "").lower()
    key = data.get("key") or {}
    message_id = key.get("id")

    # 1. Texto puro
    if "conversation" in msg and isinstance(msg["conversation"], str):
        return msg["conversation"].strip()

    if "extendedTextMessage" in msg:
        ext = msg["extendedTextMessage"]
        if isinstance(ext, dict):
            text = (ext.get("text") or "").strip()
            if text:
                return text

    # 2. Audio (PTT/voice) → Whisper
    if "audioMessage" in msg or message_type in ("audiomessage", "ptt"):
        audio_b64 = msg.get("base64") or data.get("base64")
        try:
            return await evolution.transcribe_voice(
                {"audio_base64": audio_b64, "message_id": message_id}
            )
        except Exception as exc:
            log.warning("whatsapp transcribe_voice failed", extra={"error": str(exc)})
            return "(audio recibido — no pude transcribirlo)"

    # 3. Imagen → vision
    if "imageMessage" in msg or message_type == "imagemessage":
        img_b64 = msg.get("base64") or data.get("base64")
        try:
            descripcion = await evolution.describe_image(
                {"image_base64": img_b64, "message_id": message_id}
            )
        except Exception as exc:
            log.warning("whatsapp describe_image failed", extra={"error": str(exc)})
            descripcion = "(no pude analizar la imagen)"
        caption = ""
        if isinstance(msg.get("imageMessage"), dict):
            caption = (msg["imageMessage"].get("caption") or "").strip()
        if caption:
            return f"{caption}\n\n(imagen adjunta: {descripcion})"
        return f"(imagen adjunta: {descripcion})"

    # 4. Sticker
    if "stickerMessage" in msg or message_type == "stickermessage":
        return "(el papá envió un sticker)"

    # 5. Documento
    if "documentMessage" in msg or message_type == "documentmessage":
        doc = msg.get("documentMessage") or {}
        name = (doc.get("fileName") or "archivo") if isinstance(doc, dict) else "archivo"
        return f"(el papá envió un documento: {name})"

    return ""


async def _keep_typing(evolution: EvolutionChannel, session_id: str) -> None:
    """Re-emite el indicador 'escribiendo...' mientras Sofía piensa."""
    try:
        while True:
            await evolution.typing_indicator(session_id, on=True)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
