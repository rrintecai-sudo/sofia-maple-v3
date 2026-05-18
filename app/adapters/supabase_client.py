"""Cliente Supabase (PostgREST + Storage + Auth).

Decisión: se usa sólo para features específicos de Supabase. La mayoría de
operaciones SQL pasan por PostgresAdapter (asyncpg). Ver `docs/DECISIONS.md` ADR-003.
"""

from __future__ import annotations

import logging
from typing import Any

from supabase import AsyncClient, acreate_client

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class SupabaseAdapter:
    """Wrapper sobre el cliente async oficial de Supabase."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(self.settings.supabase_url and self.settings.supabase_service_key)

    async def client(self) -> AsyncClient:
        """Lazy-init del cliente Supabase async."""
        if self._client is None:
            if not self.is_configured():
                raise RuntimeError("SUPABASE_URL o SUPABASE_SERVICE_KEY no configuradas en .env")
            self._client = await acreate_client(
                supabase_url=self.settings.supabase_url,
                supabase_key=self.settings.supabase_service_key,
            )
        return self._client

    async def health_check(self) -> dict[str, Any]:
        """Verifica reachability de PostgREST."""
        if not self.is_configured():
            return {"status": "skip", "detail": "no supabase config"}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    f"{self.settings.supabase_url}/rest/v1/",
                    headers={
                        "apikey": self.settings.supabase_service_key,
                        "Authorization": f"Bearer {self.settings.supabase_service_key}",
                    },
                )
            if resp.status_code == 200:
                return {"status": "ok"}
            if resp.status_code in (401, 403):
                return {"status": "unauthorized", "detail": f"HTTP {resp.status_code}"}
            return {"status": "unreachable", "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: SupabaseAdapter | None = None


def get_supabase() -> SupabaseAdapter:
    global _singleton
    if _singleton is None:
        _singleton = SupabaseAdapter()
    return _singleton
