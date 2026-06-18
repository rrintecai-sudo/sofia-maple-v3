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


# Espejo determinístico de `niveles_por_edad` (FIX 1, 2026-06-01). Fuente de
# verdad = la tabla; este fallback se usa si Supabase no responde, para que la
# DEDUCCIÓN de nivel/grado por edad nunca falle en el cierre de cita. Mismos
# datos que la tabla (pending validación con Cecilia, igual que la tabla).
# Tupla: (categoria, edad_min_meses, edad_max_meses, grados, nombre_display)
_NIVELES_FALLBACK: tuple[tuple[str, int, int, list[str], str], ...] = (
    ("maternal", 3, 11, [], "Cubs"),
    ("maternal", 12, 18, [], "Babies"),
    ("maternal", 18, 24, [], "Infants"),
    ("maternal", 24, 36, [], "Toddlers"),
    ("kinder", 36, 48, ["1°"], "Primero de Kinder"),
    ("kinder", 48, 60, ["2°"], "Segundo de Kinder"),
    ("kinder", 60, 72, ["3°"], "Tercero de Kinder"),
    ("primaria", 72, 84, ["1°"], "Primero de Primaria"),
    ("primaria", 84, 96, ["2°"], "Segundo de Primaria"),
    ("primaria", 96, 108, ["3°"], "Tercero de Primaria"),
    # Primaria 4-6 y Secundaria — confirmado por Lily 2026 (numeración RELATIVA al
    # nivel, una banda de un año por grado). Espejo de niveles_por_edad (ids 51-56).
    ("primaria", 108, 120, ["4°"], "Cuarto de Primaria"),
    ("primaria", 120, 132, ["5°"], "Quinto de Primaria"),
    ("primaria", 132, 144, ["6°"], "Sexto de Primaria"),
    ("secundaria", 144, 156, ["1°"], "Primero de Secundaria"),
    ("secundaria", 156, 168, ["2°"], "Segundo de Secundaria"),
    ("secundaria", 168, 180, ["3°"], "Tercero de Secundaria"),
)


async def derivar_nivel_grado_de_edad(
    edad_anos: int | None,
    *,
    nivel_preferido: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, str | None, str] | None:
    """Deduce (categoria, grado, nombre_display) de la edad. FIX 1 (2026-06-01).

    Reglas:
    - Usa `niveles_por_edad` (o el espejo `_NIVELES_FALLBACK` si Supabase falla).
    - Si la edad cae en frontera (3 años = Maternal o Kinder) y el papá NO
      especificó nivel → default al nivel más TEMPRANO (Maternal). Si SÍ
      especificó (`nivel_preferido`), se respeta.
    - Dentro de la categoría, el año se deriva por la edad exacta
      (K1=3, K2=4, K3=5). Devuelve None si no hay match (edad fuera de tabla).
    """
    if edad_anos is None:
        return None
    edad_meses = edad_anos * 12

    niveles = await listar_niveles_vigentes(settings=settings)
    if niveles:
        filas = [
            (n.categoria, n.edad_min_meses, n.edad_max_meses, list(n.grados), n.nombre_display)
            for n in niveles
        ]
    else:
        filas = [tuple(f) for f in _NIVELES_FALLBACK]  # type: ignore[misc]

    # Intervalo semi-abierto [min, max): con rangos contiguos cada edad cae en UNA
    # sola fila. Esto da K2=4 (48m), K3=5 (60m) y Primaria a los 6 (72m), porque
    # Kinder topa en K3=5 años. Fallback inclusivo para el tope de la tabla.
    cands = [f for f in filas if f[1] <= edad_meses < f[2]]
    if not cands:
        cands = [f for f in filas if f[1] <= edad_meses <= f[2]]
    if not cands:
        return None
    fila = min(cands, key=lambda f: f[1])
    categoria = fila[0]

    if nivel_preferido and nivel_preferido != categoria:
        # El papá especificó otra categoría que también cubre la edad → respétala.
        pref = [f for f in filas if f[0] == nivel_preferido and f[1] <= edad_meses <= f[2]]
        if pref:
            fila = min(pref, key=lambda f: abs(f[1] - edad_meses))
            categoria = fila[0]
    elif not nivel_preferido and categoria == "kinder" and edad_meses == 36:
        # 3 años SIN preferencia → default Maternal (Toddlers), no pre-kinder.
        mat = [f for f in filas if f[0] == "maternal" and f[1] <= 35 <= f[2]]
        if mat:
            fila = max(mat, key=lambda f: f[1])
            categoria = "maternal"

    _cat, _mn, _mx, grados, display = fila
    grado = f"{grados[0]} de {categoria.capitalize()}" if grados else None
    return categoria, grado, display


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
