"""Tool determinística para rangos de edad y niveles educativos.

Ataca el bug detectado en golden tests: Sofía a veces inventa edades (ej.
"Infants 3-12 meses" cuando es 18m-2a). El prompt v2.8 tiene los datos
correctos, pero el modelo se equivoca al re-decirlos. La tool va a Supabase
(tabla `niveles_por_edad`, migration 004) y devuelve el dato canónico.

Datos seed pending validación con Cecilia (ver `docs/AUDIT_FACTUAL_DATA.md`).
Por ahora la tool consulta `vigente = TRUE` indistintamente del flag
`confirmado_por_cliente`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NivelInfo:
    nivel: str
    nombre_display: str
    categoria: str  # 'maternal' | 'kinder' | 'primaria' | 'secundaria'
    edad_min_meses: int
    edad_max_meses: int
    grados: list[str]
    descripcion: str | None
    campus: str | None
    confirmado_por_cliente: bool = False

    @property
    def edad_min_anos(self) -> float:
        return self.edad_min_meses / 12

    @property
    def edad_max_anos(self) -> float:
        return self.edad_max_meses / 12

    def rango_legible(self) -> str:
        """Devuelve string tipo '18 meses a 2 años' o '6 a 9 años'.

        - Si edad_max < 24 meses → muestra en meses
        - Si cross-boundary (ej. 18m a 2a) → '<min> meses a <max/12> años'
        - Si >= 24 meses ambos → años
        """
        if self.edad_max_meses < 24:
            return f"{self.edad_min_meses} a {self.edad_max_meses} meses"
        if self.edad_min_meses < 24:
            anos_max = self.edad_max_meses // 12
            return f"{self.edad_min_meses} meses a {anos_max} años"
        anos_min = self.edad_min_meses // 12
        anos_max = self.edad_max_meses // 12
        return f"{anos_min} a {anos_max} años"

    def resumen_corto(self) -> str:
        return f"{self.nombre_display} ({self.rango_legible()})"


def _row_to_nivel(r: dict) -> NivelInfo:
    return NivelInfo(
        nivel=r["nivel"],
        nombre_display=r["nombre_display"],
        categoria=r["categoria"],
        edad_min_meses=int(r["edad_min_meses"]),
        edad_max_meses=int(r["edad_max_meses"]),
        grados=list(r.get("grados") or []),
        descripcion=r.get("descripcion"),
        campus=r.get("campus"),
        confirmado_por_cliente=bool(r.get("confirmado_por_cliente", False)),
    )


async def consultar_nivel_por_edad(
    edad_meses: int, *, settings: Settings | None = None
) -> NivelInfo | None:
    """Devuelve el nivel que cubre la edad dada (en meses).

    Devuelve None si Supabase no responde o ningún nivel matchea.
    """
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/niveles_por_edad",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "vigente": "eq.true",
                    "edad_min_meses": f"lte.{edad_meses}",
                    "edad_max_meses": f"gte.{edad_meses}",
                    "select": "*",
                    "limit": "1",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning(
            "consultar_nivel_por_edad failed", extra={"error": str(exc), "edad_meses": edad_meses}
        )
        return None

    if not rows:
        return None
    return _row_to_nivel(rows[0])


async def consultar_edades_de_nivel(
    nivel: str, *, settings: Settings | None = None
) -> NivelInfo | None:
    """Devuelve la info de un nivel por su key (ej. 'infants', 'preschool').

    Acepta variantes case-insensitive y matchea contra `nivel` o `nombre_display`.
    """
    settings = settings or get_settings()
    if not settings.supabase_url:
        return None

    nivel_norm = nivel.strip().lower().replace(" ", "_")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Intentar match exacto por `nivel` primero
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/niveles_por_edad",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "vigente": "eq.true",
                    "nivel": f"eq.{nivel_norm}",
                    "select": "*",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                # Fallback: buscar por substring en nombre_display
                resp = await client.get(
                    f"{settings.supabase_url}/rest/v1/niveles_por_edad",
                    headers={
                        "apikey": settings.supabase_service_key,
                        "Authorization": f"Bearer {settings.supabase_service_key}",
                    },
                    params={
                        "vigente": "eq.true",
                        "nombre_display": f"ilike.%{nivel}%",
                        "select": "*",
                        "limit": "1",
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
    except Exception as exc:
        log.warning("consultar_edades_de_nivel failed", extra={"error": str(exc), "nivel": nivel})
        return None

    if not rows:
        return None
    return _row_to_nivel(rows[0])


async def listar_niveles_vigentes(*, settings: Settings | None = None) -> list[NivelInfo]:
    """Devuelve todos los niveles vigentes ordenados por edad."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/niveles_por_edad",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "vigente": "eq.true",
                    "select": "*",
                    "order": "edad_min_meses.asc",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("listar_niveles_vigentes failed", extra={"error": str(exc)})
        return []

    return [_row_to_nivel(r) for r in rows]
