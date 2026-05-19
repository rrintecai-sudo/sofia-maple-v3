"""Query a tabla `horarios_por_nivel`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HorarioResult:
    nivel: str
    modalidad: str
    hora_inicio: str
    hora_fin: str
    dias: str
    notas: str | None = None

    def resumen_corto(self) -> str:
        return f"{self.nivel}: {self.hora_inicio} a {self.hora_fin} ({self.dias})"


async def get_horario(
    nivel: str,
    *,
    modalidad: str = "regular",
    settings: Settings | None = None,
) -> HorarioResult | None:
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/horarios_por_nivel",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "nivel": f"eq.{nivel}",
                    "modalidad": f"eq.{modalidad}",
                    "vigente": "eq.true",
                    "select": "*",
                    "limit": "1",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_horario failed", extra={"error": str(exc), "nivel": nivel})
        return None

    if not rows:
        return None
    r = rows[0]
    return HorarioResult(
        nivel=r["nivel"],
        modalidad=r["modalidad"],
        hora_inicio=str(r["hora_inicio"]),
        hora_fin=str(r["hora_fin"]),
        dias=r.get("dias", "L-V"),
        notas=r.get("notas"),
    )
