"""Query a tabla `becas`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BecaResult:
    tipo: str
    porcentaje: Decimal | None
    descripcion: str
    condiciones: str | None = None


async def get_becas(*, settings: Settings | None = None) -> list[BecaResult]:
    """Devuelve todas las becas vigentes."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/becas",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={"vigente": "eq.true", "select": "*"},
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_becas failed", extra={"error": str(exc)})
        return []

    return [
        BecaResult(
            tipo=r["tipo"],
            porcentaje=Decimal(str(r["porcentaje"])) if r.get("porcentaje") is not None else None,
            descripcion=r["descripcion"],
            condiciones=r.get("condiciones"),
        )
        for r in rows
    ]
