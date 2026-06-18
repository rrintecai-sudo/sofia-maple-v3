"""Tests del webhook de WhatsApp (Evolution API).

Cubre:
- Texto puro
- Audio (voz) → Whisper
- Imagen → vision
- fromMe=true (eco) ignorado
- Instancia equivocada ignorada
- Grupos (@g.us) ignorados
- Procesamiento en background_tasks + debounce
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from app.api.webhook_whatsapp import router as wa_router
from app.config import Settings
from app.core.orchestrator import TurnResult
from app.core.state import Canal, FaseJourney
from fastapi import FastAPI
from fastapi.testclient import TestClient


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):
    yield


@pytest.fixture
def settings_test() -> Settings:
    return Settings(
        env="test",
        evolution_base_url="https://evo.test",
        evolution_instance="sofia2-test",
        evolution_api_key="testkey",
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_key="srv-key",
    )


@pytest.fixture
def client(settings_test):
    app = FastAPI(lifespan=_noop_lifespan)
    app.include_router(wa_router)
    with (
        patch("app.api.webhook_whatsapp.get_settings", return_value=settings_test),
        TestClient(app) as c,
    ):
        yield c


def _turn_result(response: str = "Hola desde Sofía") -> TurnResult:
    return TurnResult(
        response=response,
        session_id="whatsapp:5218111234567@s.whatsapp.net",
        fase_journey=FaseJourney.BIENVENIDA,
        latency_ms=100,
        turn_number=1,
    )


def _msg_payload_texto(
    *,
    instance: str = "sofia2-test",
    from_me: bool = False,
    remote_jid: str = "5218111234567@s.whatsapp.net",
    text: str = "Hola",
) -> dict:
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "id": "ABC123",
                "fromMe": from_me,
                "remoteJid": remote_jid,
            },
            "pushName": "Oscar",
            "messageType": "conversation",
            "message": {"conversation": text},
        },
    }


# ============================================================
# 1. Texto puro — happy path
# ============================================================


def test_webhook_acepta_payload_texto(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    evolution_mock.mark_as_read = AsyncMock()
    evolution_mock.send_text = AsyncMock()
    evolution_mock.typing_indicator = AsyncMock()

    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0
    debouncer_mock.push_message = AsyncMock(return_value="seq-1")

    class _Claim:
        claimed = True
        joined = "Hola"

    debouncer_mock.try_claim = AsyncMock(return_value=_Claim())

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post("/webhook/whatsapp", json=_msg_payload_texto(text="Hola"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "received"}
    procesar_mock.assert_awaited_once()
    # canal correcto y session_id con prefijo whatsapp:
    kwargs = procesar_mock.call_args.kwargs
    assert kwargs["canal"] == Canal.WHATSAPP
    assert kwargs["session_id"].startswith("whatsapp:")
    assert kwargs["mensaje"] == "Hola"
    # respondió por Evolution
    evolution_mock.send_text.assert_awaited_once()
    # marcó como leído
    evolution_mock.mark_as_read.assert_awaited_once()


# ============================================================
# 2. fromMe=true (eco) → IGNORADO sin llamar orchestrator
# ============================================================


def test_webhook_ignora_mensaje_propio(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post(
            "/webhook/whatsapp", json=_msg_payload_texto(from_me=True, text="ignore me")
        )

    assert resp.status_code == 200
    procesar_mock.assert_not_awaited()
    evolution_mock.send_text.assert_not_called()


# ============================================================
# 3. Instancia equivocada → IGNORADO (guardar producción Sofía v1)
# ============================================================


def test_webhook_ignora_instancia_equivocada(client: TestClient) -> None:
    """🚨 CRÍTICO: si por error Evolution apunta el webhook de 'Maple Sofia'
    (v1 producción) a esta URL, NO procesamos."""
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post(
            "/webhook/whatsapp",
            json=_msg_payload_texto(instance="Maple Sofia", text="hola desde v1"),
        )

    assert resp.status_code == 200
    procesar_mock.assert_not_awaited()
    evolution_mock.send_text.assert_not_called()


# ============================================================
# 4. Eventos que no son messages.upsert → IGNORADOS silenciosamente
# ============================================================


def test_webhook_ignora_eventos_no_message(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post(
            "/webhook/whatsapp",
            json={
                "event": "connection.update",
                "instance": "sofia2-test",
                "data": {"state": "open"},
            },
        )

    assert resp.status_code == 200
    procesar_mock.assert_not_awaited()


# ============================================================
# 5. Grupos / broadcasts → IGNORADOS
# ============================================================


def test_webhook_ignora_grupos(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post(
            "/webhook/whatsapp",
            json=_msg_payload_texto(remote_jid="1234@g.us", text="grupo"),
        )

    assert resp.status_code == 200
    procesar_mock.assert_not_awaited()


# ============================================================
# 6. Audio → Whisper transcribe → procesar_turno con texto transcrito
# ============================================================


def test_webhook_audio_se_transcribe(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    evolution_mock.transcribe_voice = AsyncMock(return_value="hola transcrito")
    evolution_mock.mark_as_read = AsyncMock()
    evolution_mock.send_text = AsyncMock()
    evolution_mock.typing_indicator = AsyncMock()

    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0
    debouncer_mock.push_message = AsyncMock(return_value="seq-1")

    class _Claim:
        claimed = True
        joined = "hola transcrito"

    debouncer_mock.try_claim = AsyncMock(return_value=_Claim())

    payload = {
        "event": "messages.upsert",
        "instance": "sofia2-test",
        "data": {
            "key": {"id": "AUD1", "fromMe": False, "remoteJid": "5218111@s.whatsapp.net"},
            "messageType": "audioMessage",
            "message": {
                "audioMessage": {"ptt": True, "mimetype": "audio/ogg"},
                "base64": "FAKE_AUDIO_BASE64",
            },
        },
    }

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post("/webhook/whatsapp", json=payload)

    assert resp.status_code == 200
    evolution_mock.transcribe_voice.assert_awaited_once()
    procesar_mock.assert_awaited_once()
    assert procesar_mock.call_args.kwargs["mensaje"] == "hola transcrito"


# ============================================================
# 7. Imagen → vision describe → texto con descripción + caption
# ============================================================


def test_webhook_imagen_se_describe(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    evolution_mock.describe_image = AsyncMock(return_value="un boleto de inscripción")
    evolution_mock.mark_as_read = AsyncMock()
    evolution_mock.send_text = AsyncMock()
    evolution_mock.typing_indicator = AsyncMock()

    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0
    debouncer_mock.push_message = AsyncMock(return_value="seq-1")

    class _Claim:
        claimed = True
        joined = "Esto es lo que recibí\n\n(imagen adjunta: un boleto de inscripción)"

    debouncer_mock.try_claim = AsyncMock(return_value=_Claim())

    payload = {
        "event": "messages.upsert",
        "instance": "sofia2-test",
        "data": {
            "key": {"id": "IMG1", "fromMe": False, "remoteJid": "5218111@s.whatsapp.net"},
            "messageType": "imageMessage",
            "message": {
                "imageMessage": {"caption": "Esto es lo que recibí", "mimetype": "image/jpeg"},
                "base64": "FAKE_IMG_BASE64",
            },
        },
    }

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        resp = client.post("/webhook/whatsapp", json=payload)

    assert resp.status_code == 200
    evolution_mock.describe_image.assert_awaited_once()
    procesar_mock.assert_awaited_once()
    msg = procesar_mock.call_args.kwargs["mensaje"]
    assert "Esto es lo que recibí" in msg
    assert "boleto de inscripción" in msg


# ============================================================
# 8. Body no-JSON → 200 con status ignored (defensive)
# ============================================================


def test_webhook_body_no_json_no_revienta(client: TestClient) -> None:
    resp = client.post("/webhook/whatsapp", data="not json", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}


# ============================================================
# 9. session_id correcto del remote_jid
# ============================================================


def test_webhook_session_id_format_whatsapp(client: TestClient) -> None:
    procesar_mock = AsyncMock(return_value=_turn_result())
    evolution_mock = AsyncMock()
    evolution_mock.mark_as_read = AsyncMock()
    evolution_mock.send_text = AsyncMock()
    evolution_mock.typing_indicator = AsyncMock()

    debouncer_mock = AsyncMock()
    debouncer_mock.window_seconds = 0
    debouncer_mock.push_message = AsyncMock(return_value="seq-1")

    class _Claim:
        claimed = True
        joined = "Hola"

    debouncer_mock.try_claim = AsyncMock(return_value=_Claim())

    with (
        patch("app.api.webhook_whatsapp.procesar_turno", procesar_mock),
        patch("app.api.webhook_whatsapp.get_evolution", return_value=evolution_mock),
        patch("app.api.webhook_whatsapp.get_debouncer", return_value=debouncer_mock),
    ):
        client.post(
            "/webhook/whatsapp",
            json=_msg_payload_texto(remote_jid="5218111234567@s.whatsapp.net", text="hola"),
        )

    sid = procesar_mock.call_args.kwargs["session_id"]
    assert sid == "whatsapp:5218111234567@s.whatsapp.net"
