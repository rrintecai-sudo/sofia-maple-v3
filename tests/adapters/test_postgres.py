"""Tests del adapter Postgres."""

from __future__ import annotations

import pytest
from app.adapters.postgres_client import PostgresAdapter
from app.config import Settings


def test_is_configured() -> None:
    assert PostgresAdapter(settings=Settings()).is_configured() is False
    s = Settings(supabase_db_url="postgresql://localhost/db")
    assert PostgresAdapter(settings=s).is_configured() is True


@pytest.mark.asyncio
async def test_health_check_skip_without_url() -> None:
    adapter = PostgresAdapter(settings=Settings(supabase_db_url=""))
    result = await adapter.health_check()
    assert result["status"] == "skip"


@pytest.mark.asyncio
async def test_pool_raises_before_connect() -> None:
    adapter = PostgresAdapter(settings=Settings(supabase_db_url="postgresql://x"))
    with pytest.raises(RuntimeError, match="no inicializado"):
        _ = adapter.pool


@pytest.mark.asyncio
async def test_connect_requires_url() -> None:
    adapter = PostgresAdapter(settings=Settings(supabase_db_url=""))
    with pytest.raises(RuntimeError, match="SUPABASE_DB_URL"):
        await adapter.connect()
