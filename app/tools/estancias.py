"""Query a tabla `modalidades_estancia` (horario extendido / jornada)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

CICLO_ACTUAL = "2026-2027"

_NOMBRE_DISPLAY: dict[str, str] = {
    "manana": "Mañana",
    "media": "Media",
    "completa": "Completa",
    "express": "Express",
    "academia_individual": "Academia Individual",
}


def _fmt_hora(hhmmss: str | None) -> str | None:
    """'07:00:00' → '7:00 a.m.'; '19:00:00' → '7:00 p.m.'."""
    if not hhmmss:
        return None
    try:
        h, m, *_ = hhmmss.split(":")
        hi = int(h)
    except (ValueError, AttributeError):
        return None
    sufijo = "a.m." if hi < 12 else "p.m."
    h12 = hi % 12 or 12
    return f"{h12}:{int(m):02d} {sufijo}"


@dataclass(frozen=True)
class EstanciaResult:
    nombre: str
    aplica_para: list[str]
    hora_inicio: str | None
    hora_fin: str | None
    incluye_comida: bool
    incluye_snack: bool
    incluye_academia: bool
    costo_mensual: Decimal | None
    costo_por_dia: Decimal | None
    inscripcion_extra: Decimal | None
    notas: str | None = None

    def linea(self) -> str:
        """Una línea legible con datos REALES de la tabla (horario + costo + qué
        incluye). Las cifras las emite el CÓDIGO desde la tabla, no Haiku."""
        display = _NOMBRE_DISPLAY.get(self.nombre, self.nombre.replace("_", " ").title())
        ini = _fmt_hora(self.hora_inicio)
        fin = _fmt_hora(self.hora_fin)
        if self.costo_mensual is not None:
            costo = f"${self.costo_mensual:,.0f}/mes"
        elif self.costo_por_dia is not None:
            costo = f"${self.costo_por_dia:,.0f}/día"
        else:
            costo = "costo por confirmar"
        partes = [f"- {display}:"]
        if ini and fin:
            partes.append(f"{ini} a {fin} —")
        partes.append(costo)
        if self.notas:
            partes.append(f"({self.notas})")
        return " ".join(partes)


async def get_estancias(
    *,
    nivel: str | None = None,
    ciclo_escolar: str = CICLO_ACTUAL,
    settings: Settings | None = None,
) -> list[EstanciaResult]:
    """Modalidades de estancia vigentes. Si `nivel` se da, filtra las que aplican
    a ese nivel ('kinder'|'maternal'|'primaria_baja'|'primaria_alta'|'secundaria').
    Devuelve [] si no hay/falla (el caller defiere, no inventa)."""
    settings = settings or get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/modalidades_estancia",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "ciclo_escolar": f"eq.{ciclo_escolar}",
                    "vigente": "eq.true",
                    "select": "*",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_estancias failed", extra={"error": str(exc)})
        return []

    out: list[EstanciaResult] = []
    for r in rows:
        aplica = r.get("aplica_para") or []
        if nivel and aplica and nivel not in aplica:
            continue

        def _dec(key: str, row: dict = r) -> Decimal | None:
            v = row.get(key)
            return Decimal(str(v)) if v is not None else None

        out.append(
            EstanciaResult(
                nombre=r["nombre"],
                aplica_para=aplica,
                hora_inicio=r.get("hora_inicio"),
                hora_fin=r.get("hora_fin"),
                incluye_comida=bool(r.get("incluye_comida")),
                incluye_snack=bool(r.get("incluye_snack")),
                incluye_academia=bool(r.get("incluye_academia")),
                costo_mensual=_dec("costo_mensual"),
                costo_por_dia=_dec("costo_por_dia"),
                inscripcion_extra=_dec("inscripcion_extra"),
                notas=r.get("notas"),
            )
        )
    return out


def render_estancias_bloque(estancias: list[EstanciaResult]) -> str:
    """Bloque de texto con las modalidades (datos REALES de la tabla)."""
    if not estancias:
        return ""
    return "Modalidades de estancia (horario extendido):\n" + "\n".join(
        e.linea() for e in estancias
    )
