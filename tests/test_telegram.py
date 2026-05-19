"""Tests del TelegramChannel + webhook (con httpx mockeado vía respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.telegram_client import TelegramChannel
from app.config import Settings


def _make_settings() -> Settings:
    return Settings(
        telegram_bot_token="1234:fake_token",
        openai_api_key="sk-test",
    )


def test_is_configured() -> None:
    assert TelegramChannel(settings=_make_settings()).is_configured() is True
    assert TelegramChannel(settings=Settings(telegram_bot_token="")).is_configured() is False


def test_session_id_helpers() -> None:
    assert TelegramChannel.session_id_for_chat(123) == "telegram:123"
    assert TelegramChannel.session_id_for_chat("abc") == "telegram:abc"
    assert TelegramChannel.chat_id_from_session("telegram:456") == 456


def test_chat_id_from_session_rejects_wrong_channel() -> None:
    with pytest.raises(ValueError, match="no es de Telegram"):
        TelegramChannel.chat_id_from_session("whatsapp:123")


def test_token_property_raises_without_config() -> None:
    ch = TelegramChannel(settings=Settings(telegram_bot_token=""))
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        _ = ch.token


@pytest.mark.asyncio
@respx.mock
async def test_send_text_uses_sendMessage() -> None:
    route = respx.post("https://api.telegram.org/bot1234:fake_token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    ch = TelegramChannel(settings=_make_settings())
    await ch.send_text("telegram:99", "hola mundo")
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "hola mundo" in body
    assert '"chat_id":99' in body or '"chat_id": 99' in body
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_send_text_fallback_sin_markdown_si_falla() -> None:
    """Si sendMessage con Markdown da 400, reintenta sin parse_mode."""
    route = respx.post("https://api.telegram.org/bot1234:fake_token/sendMessage").mock(
        side_effect=[
            httpx.Response(400, json={"description": "bad markdown"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    ch = TelegramChannel(settings=_make_settings())
    await ch.send_text("telegram:99", "tex*to* roto")
    assert route.call_count == 2
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_send_sticker() -> None:
    route = respx.post("https://api.telegram.org/bot1234:fake_token/sendSticker").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    ch = TelegramChannel(settings=_make_settings())
    await ch.send_sticker("telegram:99", "CAACAgEAAxkB...")
    assert route.called
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_typing_indicator_no_falla_silenciosamente() -> None:
    """Typing es best-effort: si Telegram cae, no debe crashear."""
    respx.post("https://api.telegram.org/bot1234:fake_token/sendChatAction").mock(
        side_effect=httpx.ConnectError("network down")
    )
    ch = TelegramChannel(settings=_make_settings())
    # No debe levantar excepción
    await ch.typing_indicator("telegram:99", on=True)
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_set_webhook() -> None:
    route = respx.post("https://api.telegram.org/bot1234:fake_token/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    ch = TelegramChannel(settings=_make_settings())
    result = await ch.set_webhook("https://example.com/webhook/telegram")
    assert result.get("ok") is True
    assert route.called
    await ch.close()


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok() -> None:
    respx.get("https://api.telegram.org/bot1234:fake_token/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "TestBot"}})
    )
    ch = TelegramChannel(settings=_make_settings())
    result = await ch.health_check()
    assert result["status"] == "ok"
    assert result["detail"] == "TestBot"
    await ch.close()


@pytest.mark.asyncio
async def test_health_check_skip_sin_token() -> None:
    ch = TelegramChannel(settings=Settings(telegram_bot_token=""))
    result = await ch.health_check()
    assert result["status"] == "skip"


# ============================================================
# webhook handler
# ============================================================


@pytest.mark.asyncio
async def test_extract_text_de_mensaje_texto() -> None:
    """_extract_text devuelve el texto plano del Update."""
    from app.api.webhook_telegram import _extract_text

    ch = TelegramChannel(settings=_make_settings())
    msg = {"text": "hola sofia"}
    text = await _extract_text(msg, ch)
    assert text == "hola sofia"
    await ch.close()


@pytest.mark.asyncio
async def test_extract_text_sticker() -> None:
    from app.api.webhook_telegram import _extract_text

    ch = TelegramChannel(settings=_make_settings())
    msg = {"sticker": {"emoji": "🍁"}}
    text = await _extract_text(msg, ch)
    assert "sticker" in text.lower()
    assert "🍁" in text
    await ch.close()


@pytest.mark.asyncio
async def test_extract_text_documento() -> None:
    from app.api.webhook_telegram import _extract_text

    ch = TelegramChannel(settings=_make_settings())
    msg = {"document": {"file_name": "planeacion.pdf"}}
    text = await _extract_text(msg, ch)
    assert "planeacion.pdf" in text
    await ch.close()


@pytest.mark.asyncio
async def test_extract_text_vacio_para_update_raro() -> None:
    from app.api.webhook_telegram import _extract_text

    ch = TelegramChannel(settings=_make_settings())
    msg = {"location": {"latitude": 25.0, "longitude": -100.0}}
    text = await _extract_text(msg, ch)
    assert text == ""
    await ch.close()
