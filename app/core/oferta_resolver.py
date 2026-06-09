"""Resuelve, desde el estado de la conversación, el nivel/sub-nivel exacto para
inyectar el dato estructurado correcto de costos / horarios / estancias.

Reglas:
- COSTOS (precios_por_nivel): granularidad por NIVEL —
  'kinder' (mismo precio para sus 3 grados), 'maternal', 'primaria_baja' (1-3),
  'primaria_alta' (4-6), 'secundaria'. Si no se puede inferir 1-3 vs 4-6, None.
- HORARIOS (horarios_por_nivel): granularidad por SUB-NIVEL — kinder tiene 3
  horarios distintos (kinder_1/2/3); se resuelve por grado. Si es kinder sin grado
  → (None, necesita_grado=True): el caller pide el grado.
- ESTANCIAS: usa el nivel de precios ('kinder'|'maternal'|'primaria_baja'|...).
"""

from __future__ import annotations

from app.core.campus_resolver import (
    _infer_grado_kinder,
    _infer_grado_primaria,
    _infer_grado_secundaria,
)
from app.core.state import EstadoConversacion


def _nivel_edad_grado(estado: EstadoConversacion) -> tuple[str | None, int | None, str | None]:
    capt = estado.estado_capturado
    nivel_enum = capt.nivel_buscado_actual
    edad: int | None = None
    grado: str | None = None
    h = capt.hijo_efectivo()
    if h is not None:
        if nivel_enum is None:
            nivel_enum = h.nivel
        edad = h.edad
        grado = h.grado
    nivel = nivel_enum.value if nivel_enum is not None else None
    return nivel, edad, grado


def precio_nivel_de_estado(estado: EstadoConversacion) -> str | None:
    """→ 'kinder'|'maternal'|'primaria_baja'|'primaria_alta'|'secundaria' o None
    (None = sin nivel claro, o primaria sin grado para distinguir baja/alta)."""
    nivel, edad, grado = _nivel_edad_grado(estado)
    if nivel in ("maternal", "kinder", "secundaria"):
        return nivel
    if nivel == "primaria":
        g = _infer_grado_primaria(edad=edad, grado_texto=grado)
        if g is None:
            return None
        return "primaria_baja" if g <= 3 else "primaria_alta"
    return None


def horario_subnivel_de_estado(estado: EstadoConversacion) -> tuple[str | None, bool]:
    """→ (subnivel, necesita_grado). subnivel: 'kinder_1'..'secundaria'|'maternal'.
    necesita_grado=True cuando es kinder o primaria sin grado claro (el horario
    depende del grado exacto)."""
    nivel, edad, grado = _nivel_edad_grado(estado)
    if nivel == "maternal":
        return "maternal", False
    if nivel == "kinder":
        g = _infer_grado_kinder(edad=edad, grado_texto=grado)
        if g is None:
            return None, True  # kinder tiene 3 horarios → hay que pedir el grado
        return f"kinder_{g}", False
    if nivel == "primaria":
        g = _infer_grado_primaria(edad=edad, grado_texto=grado)
        if g is None:
            return None, True
        return ("primaria_baja" if g <= 3 else "primaria_alta"), False
    if nivel == "secundaria":
        _infer_grado_secundaria(edad=edad, grado_texto=grado)  # un solo horario
        return "secundaria", False
    return None, False
