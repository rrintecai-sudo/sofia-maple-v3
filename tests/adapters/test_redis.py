"""Tests del adapter Redis con fakeredis."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.adapters.redis_client import RedisAdapter
from app.config import Settings
from fakeredis import aioredis as fakeredis_async


@pytest.fixture
async def fake_redis_adapter():
    """RedisAdapter conectado a un fakeredis en memoria."""
    adapter = RedisAdapter(settings=Settings(redis_url="redis://fake/0"))
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    # Saltar el connect() real
    adapter._client = fake  # type: ignore[assignment]
    yield adapter
    await fake.aclose()


@pytest.mark.asyncio
async def test_push_and_pop_messages(fake_redis_adapter: RedisAdapter) -> None:
    sid = "test-session"
    await fake_redis_adapter.push_message(sid, "hola")
    await fake_redis_adapter.push_message(sid, "mundo")
    msgs = await fake_redis_adapter.pop_all_messages(sid)
    assert msgs == ["hola", "mundo"]
    # tras pop debe quedar vacío
    again = await fake_redis_adapter.pop_all_messages(sid)
    assert again == []


@pytest.mark.asyncio
async def test_set_get_with_ttl(fake_redis_adapter: RedisAdapter) -> None:
    await fake_redis_adapter.set_with_ttl("k", "v", ttl_seconds=60)
    assert await fake_redis_adapter.get("k") == "v"


@pytest.mark.asyncio
async def test_health_check_ok(fake_redis_adapter: RedisAdapter) -> None:
    result = await fake_redis_adapter.health_check()
    assert result["status"] == "ok"


def test_mask_url() -> None:
    assert RedisAdapter._mask_url("redis://:secret@host:6379") == "redis://:***@host:6379"
    # Sin password no se modifica
    assert RedisAdapter._mask_url("redis://host:6379") == "redis://host:6379"


@pytest.mark.asyncio
async def test_health_check_handles_failure() -> None:
    """Si redis falla, health_check devuelve unreachable sin lanzar."""
    adapter = RedisAdapter(settings=Settings(redis_url="redis://nonexistent-host:6379"))
    # No conectar — el health_check intentará conectar y fallará
    with patch.object(adapter, "connect", side_effect=ConnectionError("boom")):
        result = await adapter.health_check()
    assert result["status"] == "unreachable"
    assert "boom" in (result.get("detail") or "")
