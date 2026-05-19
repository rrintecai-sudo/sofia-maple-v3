"""Query a tabla `campus`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CampusResult:
    nombre: str
    direccion: str
    colonia: str | None
    ciudad: str
    niveles: list[str]
    notas: str | None = None

    def resumen_corto(self) -> str:
        ubicacion = f"{self.direccion}"
        if self.colonia:
            ubicacion += f", Col. {self.colonia}"
        return f"{self.nombre}: {ubicacion}, {self.ciudad}"


async def get_campus_para_nivel(
    nivel: str, *, settings: Settings | None = None
) -> CampusResult | None:
    """Devuelve el campus que atiende ese nivel."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/campus",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "vigente": "eq.true",
                    "select": "*",
                    "niveles": f"cs.{{{nivel}}}",  # contains nivel en el array
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_campus_para_nivel failed", extra={"error": str(exc), "nivel": nivel})
        return None

    if not rows:
        return None
    r = rows[0]
    return CampusResult(
        nombre=r["nombre"],
        direccion=r["direccion"],
        colonia=r.get("colonia"),
        ciudad=r.get("ciudad", "Saltillo"),
        niveles=list(r.get("niveles") or []),
        notas=r.get("notas"),
    )
