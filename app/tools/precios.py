"""Query a tabla `precios_por_nivel` por nivel + sub_nivel + ciclo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

CICLO_ACTUAL = "2026-2027"


@dataclass(frozen=True)
class PrecioResult:
    """Fila completa de precios para un nivel."""

    nivel: str
    sub_nivel: str | None
    ciclo_escolar: str
    inscripcion: Decimal | None
    colegiatura_mensual: Decimal | None
    seguro_escolar: Decimal | None
    seguro_orfandad: Decimal | None
    recursos_educativos: Decimal | None
    gastos_escolares: Decimal | None
    desayunos_snacks: Decimal | None
    talleres: Decimal | None
    cuota_graduacion: Decimal | None
    total_gastos_iniciales: Decimal | None
    num_colegiaturas: int
    fecha_limite_pago: str | None
    notas: str | None = None

    def resumen_corto(self) -> str:
        """Texto listo para que Sofía lo inserte en su respuesta."""
        cole = self.colegiatura_mensual or Decimal("0")
        cuotas = self.num_colegiaturas
        lines = [
            f"Colegiatura {self.nivel}: ${cole:,.0f} al mes",
            f"{cuotas} colegiaturas al año (agosto a junio).",
        ]
        if self.total_gastos_iniciales:
            lines.append(f"Gastos iniciales totales: ${self.total_gastos_iniciales:,.0f}")
        if self.notas:
            lines.append(self.notas)
        return "\n".join(lines)


async def get_precio(
    nivel: str,
    *,
    sub_nivel: str | None = None,
    ciclo_escolar: str = CICLO_ACTUAL,
    settings: Settings | None = None,
) -> PrecioResult | None:
    """Devuelve la fila vigente para el nivel + sub_nivel + ciclo. None si no existe."""
    settings = settings or get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        log.warning("get_precio: supabase no configurado")
        return None

    params = {
        "ciclo_escolar": f"eq.{ciclo_escolar}",
        "nivel": f"eq.{nivel}",
        "vigente": "eq.true",
        "select": "*",
        "limit": "1",
    }
    if sub_nivel:
        params["sub_nivel"] = f"eq.{sub_nivel}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/precios_por_nivel",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params=params,
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_precio query failed", extra={"error": str(exc), "nivel": nivel})
        return None

    if not rows:
        return None

    return _row_to_result(rows[0])


def _row_to_result(row: dict) -> PrecioResult:
    def _dec(key: str) -> Decimal | None:
        v = row.get(key)
        return Decimal(str(v)) if v is not None else None

    return PrecioResult(
        nivel=row["nivel"],
        sub_nivel=row.get("sub_nivel"),
        ciclo_escolar=row["ciclo_escolar"],
        inscripcion=_dec("inscripcion"),
        colegiatura_mensual=_dec("colegiatura_mensual"),
        seguro_escolar=_dec("seguro_escolar"),
        seguro_orfandad=_dec("seguro_orfandad"),
        recursos_educativos=_dec("recursos_educativos"),
        gastos_escolares=_dec("gastos_escolares"),
        desayunos_snacks=_dec("desayunos_snacks"),
        talleres=_dec("talleres"),
        cuota_graduacion=_dec("cuota_graduacion"),
        total_gastos_iniciales=_dec("total_gastos_iniciales"),
        num_colegiaturas=int(row.get("num_colegiaturas") or 11),
        fecha_limite_pago=row.get("fecha_limite_pago"),
        notas=row.get("notas"),
    )
