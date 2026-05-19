"""Tests de los helpers internos del orchestrator (Bloque 5.5).

Estos tests NO llaman al LLM. Solo prueban las funciones puras que ajustan
el comportamiento (decisión de campus, etc.).
"""

from __future__ import annotations

from app.core.orchestrator import _detectar_nivel_en_mensaje, _nivel_para_campus
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


# ============================================================
# _detectar_nivel_en_mensaje (Bloque 5.6 PASO 2)
# ============================================================


def test_detectar_nivel_infants() -> None:
    assert _detectar_nivel_en_mensaje("Háblame de infants") == "infants"


def test_detectar_nivel_baby() -> None:
    assert _detectar_nivel_en_mensaje("Y en baby?") == "baby"


def test_detectar_nivel_cubs() -> None:
    assert _detectar_nivel_en_mensaje("¿Háblame de cubs por favor?") == "cubs"


def test_detectar_nivel_kinder() -> None:
    assert _detectar_nivel_en_mensaje("Quiero info de kinder") == "kinder"


def test_detectar_nivel_maternal() -> None:
    assert _detectar_nivel_en_mensaje("Para maternal") == "maternal"


def test_detectar_nivel_none_si_no_hay_keyword() -> None:
    assert _detectar_nivel_en_mensaje("Hola, qué tal") is None


def test_detectar_nivel_no_substring_de_palabra() -> None:
    """No matchear 'baby' dentro de otra palabra."""
    # Caso edge: "baby" no debe matchear dentro de "babylonia" (improbable)
    assert _detectar_nivel_en_mensaje("babylonia") is None
    # Pero sí matchea cuando es palabra
    assert _detectar_nivel_en_mensaje("Mi baby tiene 1 año") == "baby"
