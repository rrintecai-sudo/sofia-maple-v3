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
        """Texto listo para que Sofía lo inserte en su respuesta.

        Fix A.3 (2026-05-19, feedback Cecilia/Gaby): NO se incluye el monto
        agregado de gastos iniciales — es demasiado para procesar de golpe.
        Solo se mencionan los conceptos en el prompt; el desglose con montos
        individuales queda en `informacion.md` como referencia interna.
        """
        cole = self.colegiatura_mensual or Decimal("0")
        cuotas = self.num_colegiaturas
        lines = [
            f"Colegiatura {self.nivel}: ${cole:,.0f} al mes",
            f"{cuotas} colegiaturas al año (agosto a junio).",
        ]
        if self.notas:
            lines.append(self.notas)
        return "\n".join(lines)

    def bloque_costos(self) -> str:
        """Frase CONVERSACIONAL con los datos REALES (sin etiqueta 'Concepto:')."""
        cole = self.colegiatura_mensual or Decimal("0")
        disp = {
            "kinder": "Kinder",
            "maternal": "Maternal",
            "secundaria": "Secundaria",
            "primaria_baja": "Primaria",
            "primaria_alta": "Primaria",
        }.get(self.nivel, self.nivel.replace("_", " ").title())
        frase = (
            f"La colegiatura de {disp} es de ${cole:,.0f} al mes "
            f"({self.num_colegiaturas} colegiaturas al año, de agosto a junio)"
        )
        if self.inscripcion is not None:
            frase += f", más una inscripción de ${self.inscripcion:,.0f}"
        # Gaby/Lili (jun 2026): mencionar SIEMPRE que hay otros gastos iniciales —
        # sin montos (eso quedó fuera por feedback de mayo), solo los conceptos.
        frase += (
            ". Además manejamos algunas cuotas iniciales como seguro escolar, recursos "
            "educativos y otras que te explicamos a detalle cuando vengas a conocernos"
        )
        return frase + "."

    def bloque_gastos_completo(self) -> str:
        """Desglose COMPLETO de gastos iniciales + total. Se usa cuando el papá los pide
        EXPLÍCITAMENTE ('cuánto son las cuotas/el seguro/el total/qué más se paga') — antes
        Sofía evadía esto y entraba en loop (queja real). Los montos vienen de la BD."""
        disp = {
            "kinder": "Kinder", "maternal": "Maternal", "secundaria": "Secundaria",
            "primaria_baja": "Primaria (1° a 3°)", "primaria_alta": "Primaria (4° a 6°)",
        }.get(self.nivel, self.nivel.replace("_", " ").title())
        conceptos: list[tuple[str, object]] = [
            ("Inscripción", self.inscripcion),
            ("Seguro escolar", self.seguro_escolar),
            ("Seguro de orfandad", self.seguro_orfandad),
            ("Recursos educativos", self.recursos_educativos),
            ("Gastos escolares", self.gastos_escolares),
            ("Desayunos y snacks", self.desayunos_snacks),
            ("Talleres", self.talleres),
        ]
        cole = self.colegiatura_mensual or Decimal("0")
        lineas = [f"💰 Te paso el detalle completo de {disp}:", f"• Colegiatura: ${cole:,.0f} al mes ({self.num_colegiaturas} al año, agosto a junio)"]
        lineas.append("")
        lineas.append("Gastos iniciales (se cubren UNA sola vez al ingresar):")
        for nombre, monto in conceptos:
            if monto:
                lineas.append(f"• {nombre}: ${Decimal(monto):,.0f}")
        if self.total_gastos_iniciales:
            lineas.append(f"\n**Total de gastos iniciales: ${self.total_gastos_iniciales:,.0f}**")
        lineas.append("\nNo hay cobros sorpresa después de esto 😊")
        return "\n".join(lineas)


async def get_todos_precios(
    *, ciclo_escolar: str = CICLO_ACTUAL, settings: Settings | None = None
) -> list[PrecioResult]:
    """Todas las filas vigentes (para la tabla compacta cuando no hay nivel claro)."""
    settings = settings or get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/precios_por_nivel",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "ciclo_escolar": f"eq.{ciclo_escolar}",
                    "vigente": "eq.true",
                    "select": "*",
                    "order": "colegiatura_mensual.asc",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("get_todos_precios failed", extra={"error": str(exc)})
        return []
    return [_row_to_result(r) for r in rows]


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
