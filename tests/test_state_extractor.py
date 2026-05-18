"""Tests del state_extractor."""

from __future__ import annotations

from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo
from app.core.state_extractor import (
    ExtraccionTurno,
    _parse_extraction,
    aplicar_extraccion,
)


def test_parse_extraction_valid_json() -> None:
    raw = '{"nivel_buscado": "primaria", "pidio_costos": true}'
    result = _parse_extraction(raw)
    assert result.nivel_buscado == "primaria"
    assert result.pidio_costos is True


def test_parse_extraction_with_backticks() -> None:
    raw = '```json\n{"nivel_buscado": "kinder"}\n```'
    result = _parse_extraction(raw)
    assert result.nivel_buscado == "kinder"


def test_parse_extraction_invalid_json_returns_empty() -> None:
    result = _parse_extraction("no es json")
    assert result.nivel_buscado is None
    assert result.pidio_costos is False


def test_aplicar_extraccion_nivel_buscado() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nivel_buscado="primaria")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nivel_buscado_actual == NivelEducativo.PRIMARIA
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].nivel == NivelEducativo.PRIMARIA


def test_aplicar_extraccion_no_sobrescribe_nombre() -> None:
    actual = EstadoCapturado(nombre_papa="Juan")
    extr = ExtraccionTurno(nombre_papa="Pedro")
    nuevo = aplicar_extraccion(actual, extr)
    # nombre original se mantiene (no sobrescribe)
    assert nuevo.nombre_papa == "Juan"


def test_aplicar_extraccion_pidio_costos_sticky() -> None:
    actual = EstadoCapturado(pidio_costos=True)
    extr = ExtraccionTurno(pidio_costos=False)
    nuevo = aplicar_extraccion(actual, extr)
    # True no se reescribe a False
    assert nuevo.pidio_costos is True


def test_aplicar_extraccion_miedos_acumula_sin_dedup() -> None:
    actual = EstadoCapturado(miedos=["bullying"])
    extr = ExtraccionTurno(miedos_nuevos=["bullying", "que no aprenda"])
    nuevo = aplicar_extraccion(actual, extr)
    assert "bullying" in nuevo.miedos
    assert "que no aprenda" in nuevo.miedos
    assert nuevo.miedos.count("bullying") == 1  # no duplica


def test_aplicar_extraccion_upsert_hijo_existente() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nombre="Mateo", nivel=NivelEducativo.PRIMARIA)])
    extr = ExtraccionTurno(
        nivel_buscado="primaria",
        edad_hijo=8,
        escuela_actual="otra escuela",
    )
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].edad == 8
    assert nuevo.hijos[0].escuela_actual == "otra escuela"
    assert nuevo.hijos[0].nombre == "Mateo"  # mantiene


def test_aplicar_extraccion_crea_nuevo_hijo_si_nivel_distinto() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nombre="Mateo", nivel=NivelEducativo.PRIMARIA)])
    extr = ExtraccionTurno(nivel_buscado="kinder", nombre_hijo="Sofía")
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 2
    nombres = {h.nombre for h in nuevo.hijos}
    assert nombres == {"Mateo", "Sofía"}


def test_aplicar_extraccion_nivel_invalido_ignora() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nivel_buscado="universidad")  # no existe
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nivel_buscado_actual is None
    assert len(nuevo.hijos) == 0


def test_aplicar_extraccion_diagnostico_no_sobrescribe() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nivel=NivelEducativo.PRIMARIA, diagnostico="autismo")])
    extr = ExtraccionTurno(nivel_buscado="primaria", diagnostico_hijo="otro")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.hijos[0].diagnostico == "autismo"
