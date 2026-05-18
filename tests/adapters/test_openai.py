"""Tests del adapter OpenAI."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.openai_client import OpenAIAdapter
from app.config import Settings


def test_is_configured() -> None:
    assert OpenAIAdapter(settings=Settings(openai_api_key="sk-x")).is_configured() is True
    assert OpenAIAdapter(settings=Settings(openai_api_key="")).is_configured() is False


@pytest.mark.asyncio
async def test_health_check_skip_when_no_key() -> None:
    adapter = OpenAIAdapter(settings=Settings(openai_api_key=""))
    assert (await adapter.health_check())["status"] == "skip"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok() -> None:
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    adapter = OpenAIAdapter(settings=Settings(openai_api_key="sk-test"))
    result = await adapter.health_check()
    assert result["status"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unauthorized() -> None:
    respx.get("https://api.openai.com/v1/models").mock(return_value=httpx.Response(401))
    adapter = OpenAIAdapter(settings=Settings(openai_api_key="sk-bad"))
    result = await adapter.health_check()
    assert result["status"] == "unauthorized"


def test_client_raises_without_key() -> None:
    adapter = OpenAIAdapter(settings=Settings(openai_api_key=""))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _ = adapter.client
