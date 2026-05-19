"""Tests de los helpers internos del orchestrator (Bloque 5.5).

Estos tests NO llaman al LLM. Solo prueban las funciones puras que ajustan
el comportamiento (decisión de campus, etc.).
"""

from __future__ import annotations

from app.core.orchestrator import _nivel_para_campus
from app.core.state import (
    EstadoConversacion,
    HijoInfo,
    NivelEducativo,
)

# ============================================================
# _nivel_para_campus (Fix 4)
# ============================================================


def test_nivel_campus_sin_estado_es_none() -> None:
    estado = EstadoConversacion.nueva("web:test")
    assert _nivel_para_campus(estado) is None


def test_nivel_campus_kinder_directo() -> None:
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.nivel_buscado_actual = NivelEducativo.KINDER
    assert _nivel_para_campus(estado) == "kinder"


def test_nivel_campus_primaria_default_a_baja_sin_edad() -> None:
    """Sin edad, 'primaria' genérica → primaria_baja (Campus 1)."""
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.nivel_buscado_actual = NivelEducativo.PRIMARIA
    assert _nivel_para_campus(estado) == "primaria_baja"


def test_nivel_campus_primaria_alta_si_edad_10_plus() -> None:
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.nivel_buscado_actual = NivelEducativo.PRIMARIA
    estado.estado_capturado.hijos = [HijoInfo(nombre="Mateo", edad=10)]
    assert _nivel_para_campus(estado) == "primaria_alta"


def test_nivel_campus_primaria_baja_si_edad_9_o_menos() -> None:
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.nivel_buscado_actual = NivelEducativo.PRIMARIA
    estado.estado_capturado.hijos = [HijoInfo(nombre="Lía", edad=7)]
    assert _nivel_para_campus(estado) == "primaria_baja"


def test_nivel_campus_secundaria() -> None:
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.nivel_buscado_actual = NivelEducativo.SECUNDARIA
    assert _nivel_para_campus(estado) == "secundaria"


def test_nivel_campus_usa_nivel_del_hijo_si_no_hay_actual() -> None:
    estado = EstadoConversacion.nueva("web:test")
    estado.estado_capturado.hijos = [HijoInfo(nivel=NivelEducativo.MATERNAL)]
    assert _nivel_para_campus(estado) == "maternal"
