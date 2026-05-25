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
    id: int | None = None
    google_maps_url: str | None = None
    estado: str | None = None

    def resumen_corto(self) -> str:
        ubicacion = f"{self.direccion}"
        if self.colonia:
            ubicacion += f", Col. {self.colonia}"
        return f"{self.nombre}: {ubicacion}, {self.ciudad}"

    def direccion_legible(self) -> str:
        """Línea de dirección completa, formato para mostrar al papá:
        'José Figueroa Siller 156, Col. Doctores, Saltillo, Coah.'"""
        partes = [self.direccion]
        if self.colonia:
            partes.append(f"Col. {self.colonia}")
        partes.append(self.ciudad)
        if self.estado:
            # Abreviamos a 'Coah.' si es Coahuila (formato común local)
            est = "Coah." if self.estado.lower().startswith("coahuila") else self.estado
            partes.append(est)
        return ", ".join(partes)


def _row_to_campus(r: dict) -> CampusResult:
    return CampusResult(
        id=int(r["id"]) if r.get("id") is not None else None,
        nombre=r["nombre"],
        direccion=r["direccion"],
        colonia=r.get("colonia"),
        ciudad=r.get("ciudad", "Saltillo"),
        niveles=list(r.get("niveles") or []),
        notas=r.get("notas"),
        google_maps_url=r.get("google_maps_url"),
        estado=r.get("estado"),
    )


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
    return _row_to_campus(rows[0])


async def get_campus_by_id(
    campus_id: int, *, settings: Settings | None = None
) -> CampusResult | None:
    """Devuelve el campus por su id (1 o 2). Usado tras resolver el campus
    desde el nivel del hijo vía `campus_resolver`."""
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
                    "id": f"eq.{campus_id}",
                    "select": "*",
                    "limit": "1",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_campus_by_id failed", extra={"error": str(exc), "id": campus_id})
        return None
    if not rows:
        return None
    return _row_to_campus(rows[0])
