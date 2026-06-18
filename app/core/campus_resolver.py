"""Mapeo determinístico nivel → campus (Bloque C.2 PASO 2).

Regla confirmada por Lily 2026-05-24:
- Campus 1: Maternal, Kinder (1°/2°/3°), Primaria 1° a 5°
- Campus 2: Primaria 6° y Secundaria (1°/2°/3°)

El campus NO se pregunta al papá — se resuelve automáticamente desde el
nivel/grado del hijo. `resolve_campus()` opera sobre keys granulares;
`resolve_campus_from_estado()` infiere la key granular desde
`EstadoConversacion` (nivel genérico + edad o grado).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.state import EstadoConversacion

log = logging.getLogger(__name__)


# Mapa granular nivel → campus_id (Lily 2026-05-24)
_CAMPUS_1_NIVELES = frozenset(
    {
        "maternal",
        "kinder_1",
        "kinder_2",
        "kinder_3",
        "primaria_1",
        "primaria_2",
        "primaria_3",
        "primaria_4",
        "primaria_5",
    }
)

_CAMPUS_2_NIVELES = frozenset(
    {
        "primaria_6",
        "secundaria_1",
        "secundaria_2",
        "secundaria_3",
    }
)

ALL_NIVELES_GRANULARES = _CAMPUS_1_NIVELES | _CAMPUS_2_NIVELES


def resolve_campus(nivel: str) -> int:
    """Retorna campus_id (1 o 2) según el nivel granular del hijo.

    Args:
        nivel: key granular como 'maternal', 'kinder_1', 'primaria_5', 'secundaria_2'.

    Returns:
        1 o 2.

    Raises:
        ValueError: si el nivel no está en el mapa (input fuera de los
            13 valores conocidos).
    """
    n = nivel.strip().lower()
    if n in _CAMPUS_1_NIVELES:
        return 1
    if n in _CAMPUS_2_NIVELES:
        return 2
    raise ValueError(
        f"nivel granular desconocido: {nivel!r}. Esperado uno de: {sorted(ALL_NIVELES_GRANULARES)}"
    )


# ============================================================
# Helpers de inferencia desde estado (nivel genérico + edad/grado)
# ============================================================


def _infer_grado_primaria(*, edad: int | None, grado_texto: str | None) -> int | None:
    """Devuelve grado (1-6) o None si no puede inferirse.

    Reglas:
    - Si grado_texto contiene un dígito 1-6 → ese grado.
    - Si grado_texto tiene un ordinal escrito (primero, segundo, …) → ese.
    - Si solo hay edad: 6→1°, 7→2°, 8→3°, 9→4°, 10→5°, 11→6°.
    """
    if grado_texto:
        # Detección numérica directa (1ro, 2do, 3ero, 4to, 5to, 6to)
        m = re.search(r"(?<!\d)([1-6])(?!\d)", grado_texto)
        if m:
            return int(m.group(1))
        # Ordinales escritos
        ordinales = {
            "primer": 1,
            "primero": 1,
            "primera": 1,
            "segundo": 2,
            "segunda": 2,
            "tercer": 3,
            "tercero": 3,
            "tercera": 3,
            "cuarto": 4,
            "cuarta": 4,
            "quinto": 5,
            "quinta": 5,
            "sexto": 6,
            "sexta": 6,
        }
        g_low = grado_texto.lower()
        for palabra, n in ordinales.items():
            if palabra in g_low:
                return n
    if edad is not None:
        if 6 <= edad <= 11:
            return edad - 5  # 6→1, 7→2, …, 11→6
    return None


def _infer_grado_kinder(*, edad: int | None, grado_texto: str | None) -> int | None:
    """Devuelve grado 1/2/3 de Kinder, o None."""
    if grado_texto:
        m = re.search(r"(?<!\d)([1-3])(?!\d)", grado_texto)
        if m:
            return int(m.group(1))
        ordinales = {
            "primer": 1,
            "primero": 1,
            "primera": 1,
            "segundo": 2,
            "segunda": 2,
            "tercer": 3,
            "tercero": 3,
            "tercera": 3,
        }
        g_low = grado_texto.lower()
        for palabra, n in ordinales.items():
            if palabra in g_low:
                return n
    if edad is not None:
        # Kinder estándar: 3 años → K1, 4 → K2, 5 → K3
        if 3 <= edad <= 5:
            return edad - 2
    return None


def _infer_grado_secundaria(*, edad: int | None, grado_texto: str | None) -> int | None:
    if grado_texto:
        m = re.search(r"(?<!\d)([1-3])(?!\d)", grado_texto)
        if m:
            return int(m.group(1))
        ordinales = {"primer": 1, "primero": 1, "segundo": 2, "tercer": 3, "tercero": 3}
        g_low = grado_texto.lower()
        for palabra, n in ordinales.items():
            if palabra in g_low:
                return n
    if edad is not None:
        if 12 <= edad <= 14:
            return edad - 11
    return None


def resolve_campus_from_estado(estado: EstadoConversacion) -> int | None:
    """Resuelve campus_id desde el estado capturado.

    Estrategia (en orden):
      1. Si hay nivel_buscado_actual o nivel del primer hijo, lo combina
         con edad/grado para obtener key granular y delegar a `resolve_campus`.
      2. Si no se puede inferir grado en primaria (necesita 1-5 vs 6),
         devuelve None — el caller debe pedir más info.

    Args:
        estado: EstadoConversacion.

    Returns:
        1, 2, o None si no hay datos suficientes.
    """
    capt = estado.estado_capturado
    nivel_enum = capt.nivel_buscado_actual
    edad = None
    grado_texto = None
    h0 = capt.hijo_efectivo()  # FIX (d): hijo consolidado, no hijos[0] a ciegas
    if h0 is not None:
        if nivel_enum is None:
            nivel_enum = h0.nivel
        edad = h0.edad
        grado_texto = h0.grado

    if nivel_enum is None:
        return None
    nivel_val = nivel_enum.value if hasattr(nivel_enum, "value") else str(nivel_enum)

    if nivel_val == "maternal":
        return resolve_campus("maternal")

    if nivel_val == "kinder":
        grado = _infer_grado_kinder(edad=edad, grado_texto=grado_texto)
        # Todos los grados de Kinder son Campus 1 — si no podemos inferir grado,
        # de todas formas es Campus 1.
        return resolve_campus(f"kinder_{grado or 1}")

    if nivel_val == "primaria":
        grado = _infer_grado_primaria(edad=edad, grado_texto=grado_texto)
        if grado is None:
            # Ambiguo: no sabemos si es 1-5 (Campus 1) o 6 (Campus 2).
            log.info("resolve_campus_from_estado primaria sin grado → None")
            return None
        return resolve_campus(f"primaria_{grado}")

    if nivel_val == "secundaria":
        grado = _infer_grado_secundaria(edad=edad, grado_texto=grado_texto)
        # Todos los grados de Secundaria son Campus 2 — si no se infiere,
        # asumimos Campus 2 (no hay riesgo).
        return resolve_campus(f"secundaria_{grado or 1}")

    log.warning("resolve_campus_from_estado nivel desconocido: %r", nivel_val)
    return None
