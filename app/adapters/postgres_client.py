"""Cliente Postgres async con pool de conexiones.

Decisión: asyncpg directo, no supabase-py. Ver `docs/DECISIONS.md` ADR-003.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class PostgresAdapter:
    """Wrapper con pool sobre asyncpg."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool: asyncpg.Pool | None = None

    def is_configured(self) -> bool:
        return bool(self.settings.supabase_db_url)

    async def connect(
        self,
        min_size: int = 1,
        max_size: int = 10,
        command_timeout: float = 30.0,
    ) -> None:
        """Inicializa el pool de conexiones. Llamar UNA vez al arranque."""
        if self._pool is not None:
            return
        if not self.is_configured():
            raise RuntimeError(
                "SUPABASE_DB_URL no está configurada. "
                "Sácala del Dashboard → Database → Connection String → URI."
            )
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.supabase_db_url,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
        )
        log.info("postgres pool initialized", extra={"min_size": min_size, "max_size": max_size})

    async def disconnect(self) -> None:
        """Cierra el pool. Llamar al shutdown."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            log.info("postgres pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Postgres pool no inicializado. Llama a connect() primero.")
        return self._pool

    async def fetch_one(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def health_check(self) -> dict[str, Any]:
        """Verifica conexión con `SELECT 1`."""
        if not self.is_configured():
            return {"status": "skip", "detail": "no db url configured"}
        try:
            if self._pool is None:
                await self.connect()
            result = await self.fetch_one("SELECT 1 as ok")
            if result and result["ok"] == 1:
                return {"status": "ok"}
            return {"status": "unreachable", "detail": "unexpected response"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: PostgresAdapter | None = None


def get_postgres() -> PostgresAdapter:
    global _singleton
    if _singleton is None:
        _singleton = PostgresAdapter()
    return _singleton
