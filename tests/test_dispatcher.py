"""Tests del dispatcher de canales (Bloque C.1 PASO 7).

Identifica el canal del session_id y envía el mensaje vía el adapter
correcto. Web persiste como assistant message (no hay push).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.dispatcher import send_message_to_session

# ============================================================
# Telegram
# ============================================================


@pytest.mark.asyncio
async def test_dispatcher_telegram_llama_send_text() -> None:
    mock_telegram = AsyncMock()
    mock_telegram.send_text = AsyncMock()
    with patch("app.adapters.telegram_client.get_telegram", return_value=mock_telegram):
        result = await send_message_to_session("telegram:123", "hola")
    assert result == {"sent": True, "channel": "telegram", "detail": None}
    mock_telegram.send_text.assert_called_once_with("telegram:123", "hola")


@pytest.mark.asyncio
async def test_dispatcher_telegram_error_devuelve_sent_false() -> None:
    mock_telegram = AsyncMock()
    mock_telegram.send_text = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("app.adapters.telegram_client.get_telegram", return_value=mock_telegram):
        result = await send_message_to_session("telegram:123", "hola")
    assert result["sent"] is False
    assert result["channel"] == "telegram"
    assert "api down" in result["detail"]


# ============================================================
# WhatsApp / Evolution
# ============================================================


@pytest.mark.asyncio
async def test_dispatcher_whatsapp_llama_evolution() -> None:
    mock_evo = AsyncMock()
    mock_evo.send_text = AsyncMock()
    with patch("app.adapters.evolution_client.get_evolution", return_value=mock_evo):
        result = await send_message_to_session("whatsapp:5218441302112", "hola")
    assert result == {"sent": True, "channel": "whatsapp", "detail": None}
    mock_evo.send_text.assert_called_once_with("whatsapp:5218441302112", "hola")


@pytest.mark.asyncio
async def test_dispatcher_whatsapp_error_devuelve_sent_false() -> None:
    mock_evo = AsyncMock()
    mock_evo.send_text = AsyncMock(side_effect=RuntimeError("evolution down"))
    with patch("app.adapters.evolution_client.get_evolution", return_value=mock_evo):
        result = await send_message_to_session("whatsapp:5218441302112", "hola")
    assert result["sent"] is False


# ============================================================
# Web (sin push real — persiste como assistant message)
# ============================================================


@pytest.mark.asyncio
async def test_dispatcher_web_persiste_como_assistant_message() -> None:
    mock_repo = AsyncMock()
    mock_repo.insert_message = AsyncMock()
    with patch("app.core.repository.get_repository", return_value=mock_repo):
        result = await send_message_to_session("web:abc-uuid", "hola")
    assert result["sent"] is True
    assert result["channel"] == "web"
    assert result["detail"] == "persisted_for_next_visit"
    mock_repo.insert_message.assert_called_once_with(
        session_id="web:abc-uuid", role="assistant", content="hola"
    )


# ============================================================
# session_id inválido
# ============================================================


@pytest.mark.asyncio
async def test_dispatcher_session_id_invalido() -> None:
    result = await send_message_to_session("no-prefix", "hola")
    assert result["sent"] is False
    assert "inválido" in result["detail"]


@pytest.mark.asyncio
async def test_dispatcher_canal_desconocido() -> None:
    """Si el session_id tiene un prefijo que parse_session_id no acepta,
    devuelve sent=False con detail apropiado."""
    result = await send_message_to_session("slack:xyz", "hola")
    assert result["sent"] is False
