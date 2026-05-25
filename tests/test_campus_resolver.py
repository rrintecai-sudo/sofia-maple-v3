"""Tests de campus_resolver (Bloque C.2 PASO 2).

Cubre:
- resolve_campus(nivel) para los 13 niveles canónicos
- resolve_campus_from_estado() infiriendo grado desde edad o grado_texto
- casos ambiguos (primaria sin grado) → None
"""

from __future__ import annotations

import pytest
from app.core.campus_resolver import (
    ALL_NIVELES_GRANULARES,
    resolve_campus,
    resolve_campus_from_estado,
)
from app.core.state import (
    Canal,
    EstadoCapturado,
    EstadoConversacion,
    HijoInfo,
    NivelEducativo,
)

# ============================================================
# resolve_campus — 13 casos canónicos
# ============================================================


@pytest.mark.parametrize(
    "nivel,esperado",
    [
        ("maternal", 1),
        ("kinder_1", 1),
        ("kinder_2", 1),
        ("kinder_3", 1),
        ("primaria_1", 1),
        ("primaria_2", 1),
        ("primaria_3", 1),
        ("primaria_4", 1),
        ("primaria_5", 1),
        ("primaria_6", 2),
        ("secundaria_1", 2),
        ("secundaria_2", 2),
        ("secundaria_3", 2),
    ],
)
def test_resolve_campus_13_casos(nivel: str, esperado: int) -> None:
    assert resolve_campus(nivel) == esperado


def test_resolve_campus_case_insensitive() -> None:
    assert resolve_campus("MATERNAL") == 1
    assert resolve_campus("Primaria_6") == 2


def test_resolve_campus_strips_whitespace() -> None:
    assert resolve_campus("  kinder_2  ") == 1


def test_resolve_campus_nivel_desconocido_lanza() -> None:
    with pytest.raises(ValueError, match="nivel granular desconocido"):
        resolve_campus("prepa_1")
    with pytest.raises(ValueError):
        resolve_campus("kinder_4")  # no existe el 4°


def test_all_niveles_son_13() -> None:
    """Sanity: 9 niveles Campus 1 + 4 Campus 2 = 13."""
    assert len(ALL_NIVELES_GRANULARES) == 13


# ============================================================
# resolve_campus_from_estado — inferencia desde estado
# ============================================================


def _estado_con(*, nivel: NivelEducativo, edad: int | None = None, grado: str | None = None):
    return EstadoConversacion(
        session_id="web:test",
        canal=Canal.WEB,
        identificador="test",
        estado_capturado=EstadoCapturado(
            nivel_buscado_actual=nivel,
            hijos=[HijoInfo(edad=edad, nivel=nivel, grado=grado)] if (edad or grado) else [],
        ),
    )


def test_estado_maternal_siempre_campus_1() -> None:
    e = _estado_con(nivel=NivelEducativo.MATERNAL)
    assert resolve_campus_from_estado(e) == 1


def test_estado_kinder_sin_grado_es_campus_1() -> None:
    """Cualquier grado de Kinder es Campus 1 — si falta grado, igual."""
    e = _estado_con(nivel=NivelEducativo.KINDER)
    assert resolve_campus_from_estado(e) == 1


def test_estado_kinder_por_edad_5() -> None:
    e = _estado_con(nivel=NivelEducativo.KINDER, edad=5)
    assert resolve_campus_from_estado(e) == 1


def test_estado_primaria_grado_1_por_texto() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, grado="1ro de primaria")
    assert resolve_campus_from_estado(e) == 1


def test_estado_primaria_grado_5_por_texto() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, grado="5to primaria")
    assert resolve_campus_from_estado(e) == 1


def test_estado_primaria_grado_6_por_texto_es_campus_2() -> None:
    """El caso crítico: 6° primaria cambia a Campus 2."""
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, grado="6to primaria")
    assert resolve_campus_from_estado(e) == 2


def test_estado_primaria_por_edad_10_es_5to() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, edad=10)
    assert resolve_campus_from_estado(e) == 1


def test_estado_primaria_por_edad_11_es_6to_campus_2() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, edad=11)
    assert resolve_campus_from_estado(e) == 2


def test_estado_primaria_sin_grado_ni_edad_es_none() -> None:
    """Ambiguo: no podemos saber si es 1-5 (C1) o 6 (C2)."""
    e = _estado_con(nivel=NivelEducativo.PRIMARIA)
    assert resolve_campus_from_estado(e) is None


def test_estado_primaria_ordinal_escrito_sexto() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, grado="sexto")
    assert resolve_campus_from_estado(e) == 2


def test_estado_primaria_ordinal_escrito_tercero_campus_1() -> None:
    e = _estado_con(nivel=NivelEducativo.PRIMARIA, grado="tercero de primaria")
    assert resolve_campus_from_estado(e) == 1


def test_estado_secundaria_sin_grado_es_campus_2() -> None:
    e = _estado_con(nivel=NivelEducativo.SECUNDARIA)
    assert resolve_campus_from_estado(e) == 2


def test_estado_secundaria_por_edad_13() -> None:
    e = _estado_con(nivel=NivelEducativo.SECUNDARIA, edad=13)
    assert resolve_campus_from_estado(e) == 2


def test_estado_sin_nivel_es_none() -> None:
    """Sin nivel capturado, no se puede resolver."""
    e = EstadoConversacion(
        session_id="web:test",
        canal=Canal.WEB,
        identificador="test",
        estado_capturado=EstadoCapturado(),
    )
    assert resolve_campus_from_estado(e) is None


def test_estado_toma_nivel_del_hijo_si_no_hay_buscado() -> None:
    """Si nivel_buscado_actual=None pero el hijo tiene nivel, usa ese."""
    e = EstadoConversacion(
        session_id="web:test",
        canal=Canal.WEB,
        identificador="test",
        estado_capturado=EstadoCapturado(
            nivel_buscado_actual=None,
            hijos=[HijoInfo(nivel=NivelEducativo.MATERNAL, edad=1)],
        ),
    )
    assert resolve_campus_from_estado(e) == 1
