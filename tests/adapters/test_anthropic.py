"""Tests del adapter Anthropic (con httpx mockeado vía respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.anthropic_client import AnthropicAdapter
from app.config import Settings


def test_is_configured_with_key() -> None:
    s = Settings(anthropic_api_key="sk-ant-test")
    adapter = AnthropicAdapter(settings=s)
    assert adapter.is_configured() is True


def test_is_configured_without_key() -> None:
    s = Settings(anthropic_api_key="")
    adapter = AnthropicAdapter(settings=s)
    assert adapter.is_configured() is False


@pytest.mark.asyncio
async def test_health_check_skip_when_no_key() -> None:
    s = Settings(anthropic_api_key="")
    adapter = AnthropicAdapter(settings=s)
    result = await adapter.health_check()
    assert result["status"] == "skip"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok_with_valid_key() -> None:
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    s = Settings(anthropic_api_key="sk-ant-fake")
    adapter = AnthropicAdapter(settings=s)
    result = await adapter.health_check()
    assert result["status"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unauthorized() -> None:
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "invalid"})
    )
    s = Settings(anthropic_api_key="sk-ant-bad")
    adapter = AnthropicAdapter(settings=s)
    result = await adapter.health_check()
    assert result["status"] == "unauthorized"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unreachable() -> None:
    respx.get("https://api.anthropic.com/v1/models").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    s = Settings(anthropic_api_key="sk-ant-test")
    adapter = AnthropicAdapter(settings=s)
    result = await adapter.health_check()
    assert result["status"] == "unreachable"


def test_client_property_raises_without_key() -> None:
    s = Settings(anthropic_api_key="")
    adapter = AnthropicAdapter(settings=s)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _ = adapter.client
