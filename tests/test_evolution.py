"""Tests del EvolutionChannel (WhatsApp adapter — código listo, sin número real)."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.evolution_client import EvolutionChannel
from app.config import Settings


def _make_settings() -> Settings:
    return Settings(
        evolution_base_url="https://evo.example.com",
        evolution_instance="maple-test",
        evolution_api_key="evo-key",
        openai_api_key="sk-test",
    )


def test_is_configured() -> None:
    assert EvolutionChannel(settings=_make_settings()).is_configured() is True
    assert EvolutionChannel(settings=Settings()).is_configured() is False


def test_session_id_helpers() -> None:
    sid = EvolutionChannel.session_id_for_remote("5218441302112@s.whatsapp.net")
    assert sid == "whatsapp:5218441302112@s.whatsapp.net"
    assert EvolutionChannel.remote_jid_from_session(sid) == "5218441302112@s.whatsapp.net"


def test_session_id_rechaza_canal_invalido() -> None:
    with pytest.raises(ValueError, match="no es de WhatsApp"):
        EvolutionChannel.remote_jid_from_session("telegram:123")


def test_number_from_remote_jid() -> None:
    assert (
        EvolutionChannel.number_from_remote_jid("5218441302112@s.whatsapp.net") == "5218441302112"
    )
    assert EvolutionChannel.number_from_remote_jid("5218441302112@c.us") == "5218441302112"


@pytest.mark.asyncio
@respx.mock
async def test_send_text() -> None:
    route = respx.post("https://evo.example.com/message/sendText/maple-test").mock(
        return_value=httpx.Response(200, json={"key": {"id": "abc"}})
    )
    ch = EvolutionChannel(settings=_make_settings())
    await ch.send_text("whatsapp:5218441302112@s.whatsapp.net", "hola")
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "5218441302112" in body
    assert "hola" in body
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_send_image_con_caption() -> None:
    route = respx.post("https://evo.example.com/message/sendMedia/maple-test").mock(
        return_value=httpx.Response(200)
    )
    ch = EvolutionChannel(settings=_make_settings())
    await ch.send_image(
        "whatsapp:5218441302112@s.whatsapp.net",
        "https://example.com/img.jpg",
        caption="costos kinder",
    )
    body = route.calls.last.request.content.decode()
    assert "image/jpeg" in body
    assert "costos kinder" in body
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_send_sticker() -> None:
    route = respx.post("https://evo.example.com/message/sendSticker/maple-test").mock(
        return_value=httpx.Response(200)
    )
    ch = EvolutionChannel(settings=_make_settings())
    await ch.send_sticker("whatsapp:5218441302112@s.whatsapp.net", "https://x.com/sticker.webp")
    assert route.called
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_typing_indicator_no_falla() -> None:
    respx.post("https://evo.example.com/chat/sendPresence/maple-test").mock(
        side_effect=httpx.ConnectError("network down")
    )
    ch = EvolutionChannel(settings=_make_settings())
    # No debe levantar excepción (es best-effort)
    await ch.typing_indicator("whatsapp:5218441302112@s.whatsapp.net", on=True)
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok_si_instance_open() -> None:
    respx.get("https://evo.example.com/instance/connectionState/maple-test").mock(
        return_value=httpx.Response(200, json={"instance": {"state": "open"}})
    )
    ch = EvolutionChannel(settings=_make_settings())
    r = await ch.health_check()
    assert r["status"] == "ok"
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unreachable_si_instance_closed() -> None:
    respx.get("https://evo.example.com/instance/connectionState/maple-test").mock(
        return_value=httpx.Response(200, json={"instance": {"state": "close"}})
    )
    ch = EvolutionChannel(settings=_make_settings())
    r = await ch.health_check()
    assert r["status"] == "unreachable"
    await ch.close()


@pytest.mark.asyncio
async def test_health_check_skip_sin_config() -> None:
    ch = EvolutionChannel(settings=Settings())
    r = await ch.health_check()
    assert r["status"] == "skip"


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_voice_con_audio_base64_inline() -> None:
    """Si Evolution mandó audio_base64 en el webhook, no llama getBase64FromMediaMessage."""
    import base64

    fake_audio = base64.b64encode(b"fake-ogg-bytes").decode("ascii")
    respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hola buenos días"})
    )
    ch = EvolutionChannel(settings=_make_settings())
    result = await ch.transcribe_voice({"audio_base64": fake_audio})
    assert result == "hola buenos días"
    await ch.close()


@pytest.mark.asyncio
async def test_transcribe_voice_sin_payload_falla() -> None:
    ch = EvolutionChannel(settings=_make_settings())
    with pytest.raises(ValueError, match="audio_base64"):
        await ch.transcribe_voice({})
    await ch.close()
