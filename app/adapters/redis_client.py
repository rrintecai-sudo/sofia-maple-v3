"""Cliente Redis async (debounce 7s para mensajes en cadena)."""

from __future__ import annotations

import logging
from typing import Any

from redis.asyncio import Redis, from_url

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class RedisAdapter:
    """Wrapper sobre redis.asyncio.Redis."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Redis | None = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = from_url(  # type: ignore[no-untyped-call]
            self.settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        # ping para verificar
        await self._client.ping()
        log.info("redis connected", extra={"url": self._mask_url(self.settings.redis_url)})

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis no inicializado. Llama a connect() primero.")
        return self._client

    async def push_message(self, session_id: str, payload: str) -> int:
        """Añade un mensaje a la cola del session_id (RPUSH)."""
        return await self.client.rpush(f"session:{session_id}", payload)  # type: ignore[no-any-return]

    async def pop_all_messages(self, session_id: str) -> list[str]:
        """Saca todos los mensajes de la cola (atómicamente) y borra la key."""
        key = f"session:{session_id}"
        pipe = self.client.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
        return list(results[0])

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        await self.client.set(key, value, ex=ttl_seconds)

    async def get(self, key: str) -> str | None:
        return await self.client.get(key)  # type: ignore[no-any-return]

    async def health_check(self) -> dict[str, Any]:
        """PING al servidor."""
        try:
            if self._client is None:
                await self.connect()
            pong = await self.client.ping()
            return {"status": "ok"} if pong else {"status": "unreachable", "detail": "no pong"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}

    @staticmethod
    def _mask_url(url: str) -> str:
        """Oculta password en URL para logs (`redis://:PASS@host` → `redis://:***@host`)."""
        if "@" not in url or ":" not in url.split("@")[0]:
            return url
        scheme_and_creds, host = url.rsplit("@", 1)
        scheme, _, _ = scheme_and_creds.partition(":")
        return f"{scheme}://:***@{host}"


_singleton: RedisAdapter | None = None


def get_redis() -> RedisAdapter:
    global _singleton
    if _singleton is None:
        _singleton = RedisAdapter()
    return _singleton
