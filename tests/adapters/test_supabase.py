"""Tests del adapter Supabase."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.adapters.supabase_client import SupabaseAdapter
from app.config import Settings


def test_is_configured() -> None:
    assert SupabaseAdapter(settings=Settings()).is_configured() is False
    full = Settings(supabase_url="https://x.supabase.co", supabase_service_key="key")
    assert SupabaseAdapter(settings=full).is_configured() is True


@pytest.mark.asyncio
async def test_health_check_skip() -> None:
    assert (await SupabaseAdapter(settings=Settings()).health_check())["status"] == "skip"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok() -> None:
    respx.get("https://x.supabase.co/rest/v1/").mock(return_value=httpx.Response(200))
    settings = Settings(supabase_url="https://x.supabase.co", supabase_service_key="sk")
    result = await SupabaseAdapter(settings=settings).health_check()
    assert result["status"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unauthorized() -> None:
    respx.get("https://x.supabase.co/rest/v1/").mock(return_value=httpx.Response(401))
    settings = Settings(supabase_url="https://x.supabase.co", supabase_service_key="bad")
    result = await SupabaseAdapter(settings=settings).health_check()
    assert result["status"] == "unauthorized"
