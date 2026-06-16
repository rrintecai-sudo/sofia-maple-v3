"""Query a tabla `horarios_por_nivel`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


# Nombre legible del sub-nivel para el bloque inyectado.
_NIVEL_DISPLAY: dict[str, str] = {
    "premater": "Premater",
    "maternal": "Maternal",
    "kinder_1": "1° de Kinder",
    "kinder_2": "2° de Kinder",
    "kinder_3": "3° de Kinder",
    "primaria_baja": "1° a 3° de Primaria",
    "primaria_alta": "4° a 6° de Primaria",
    "secundaria": "Secundaria",
}


def _fmt_hora(hhmmss: str) -> str:
    """'09:00:00' → '9:00 a.m.'; '14:00:00' → '2:00 p.m.'."""
    try:
        h, m, *_ = str(hhmmss).split(":")
        hi = int(h)
    except (ValueError, AttributeError):
        return str(hhmmss)
    sufijo = "a.m." if hi < 12 else "p.m."
    h12 = hi % 12 or 12
    return f"{h12}:{int(m):02d} {sufijo}"


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

    def bloque(self) -> str:
        """Frase CONVERSACIONAL (sin etiqueta 'Horario de X:')."""
        display = _NIVEL_DISPLAY.get(self.nivel, self.nivel)
        dias = "lunes a viernes" if self.dias in ("L-V", "lun-vie") else self.dias
        return (
            f"En {display} las clases son de {_fmt_hora(self.hora_inicio)} a "
            f"{_fmt_hora(self.hora_fin)}, de {dias}."
        )


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
