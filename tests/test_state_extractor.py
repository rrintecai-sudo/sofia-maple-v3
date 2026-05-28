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


# ============================================================
# Fix B.1 (2026-05-19, reunión Maple): cantidad_hijos vs edad_hijo
#
# Bug: el extractor LLM confundía "tengo 4 hijos" con "tiene 4 años".
# Tests usan _parse_extraction con JSON simulado — testean el schema
# y que `cantidad_hijos` sea campo separado de `edad_hijo`.
#
# La calidad del prompt LLM (few-shot) se valida en golden tests
# con conversación real cuando se redeploye.
# ============================================================


def test_extraccion_acepta_cantidad_hijos_separado() -> None:
    """Schema permite cantidad_hijos sin tocar edad_hijo (bug B.1)."""
    raw = '{"cantidad_hijos": 4, "edad_hijo": null}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos == 4
    assert result.edad_hijo is None


def test_extraccion_acepta_edad_hijo_sin_cantidad() -> None:
    """'Mi hijo tiene 4 años' → edad_hijo=4, cantidad_hijos=null."""
    raw = '{"cantidad_hijos": null, "edad_hijo": 4}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos is None
    assert result.edad_hijo == 4


def test_extraccion_ambiguo_ambos_null() -> None:
    """Mensaje ambiguo '4' sin contexto → ambos null (Sofía pregunta)."""
    raw = '{"cantidad_hijos": null, "edad_hijo": null}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos is None
    assert result.edad_hijo is None


def test_extraccion_cantidad_hijos_validacion_rango() -> None:
    """cantidad_hijos debe estar en 0-10. Valor fuera de rango → fallback."""
    raw = '{"cantidad_hijos": 50}'
    result = _parse_extraction(raw)
    # Pydantic rechaza → fallback a ExtraccionTurno() vacío
    assert result.cantidad_hijos is None


def test_aplicar_no_pone_cantidad_hijos_como_edad() -> None:
    """`cantidad_hijos` NO se copia a edad del hijo — fix del bug raíz.

    Si LLM extrae solo cantidad_hijos=4 (papá dijo 'tengo 4 hijos'),
    el estado NO debe terminar con edad=4 en ningún hijo, y NO debe
    crearse un HijoInfo solo por la cantidad.
    """
    actual = EstadoCapturado()
    extr = ExtraccionTurno(cantidad_hijos=4)
    nuevo = aplicar_extraccion(actual, extr)
    # NO se crea hijo solo por cantidad (no hay otro dato de hijo)
    assert len(nuevo.hijos) == 0
    # Y obviamente ninguno tiene edad=4
    assert all(h.edad != 4 for h in nuevo.hijos)


def test_aplicar_edad_hijo_correcto_si_es_edad() -> None:
    """Si el LLM mete edad_hijo=4 (papá dijo 'tiene 4 años'), sí se aplica."""
    actual = EstadoCapturado()
    extr = ExtraccionTurno(edad_hijo=4)
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].edad == 4


# ============================================================
# Fix C.1.A — extractor debe capturar nombre_papa (faltaba regla + few-shot)
# Bug detectado en prod 2026-05-25: papá dijo "Me llamo Oscar Rodriguez"
# y nombre_papa quedó None → handler de agendado no pudo crear lead
# (parent_name NOT NULL) → Sofía alucinó la confirmación.
# ============================================================


def test_system_prompt_documenta_nombre_papa() -> None:
    """El system prompt enumera explícitamente nombre_papa en sus reglas y
    contiene ejemplos few-shot."""
    from app.core.state_extractor import _SYSTEM_PROMPT

    prompt_low = _SYSTEM_PROMPT.lower()
    # Regla
    assert "nombre_papa" in _SYSTEM_PROMPT
    # Patrones canónicos
    for pat in ["me llamo", "soy ", "mi nombre es", "habla la mamá"]:
        assert pat in prompt_low, f"few-shot patrón ausente: {pat!r}"
    # Disambiguación contra nombre_hijo
    assert "nombre del hijo" in prompt_low or "nombre_hijo" in _SYSTEM_PROMPT


def test_parse_extraction_nombre_papa() -> None:
    """Plumbing: si el LLM devuelve nombre_papa, el parser lo conserva."""
    raw = '{"nombre_papa": "Oscar Rodriguez", "nivel_buscado": "kinder", "edad_hijo": 5}'
    result = _parse_extraction(raw)
    assert result.nombre_papa == "Oscar Rodriguez"
    assert result.nivel_buscado == "kinder"
    assert result.edad_hijo == 5


def test_aplicar_extraccion_nombre_papa_nuevo() -> None:
    """Si nombre_papa estaba None, se aplica el nuevo."""
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nombre_papa="Oscar Rodriguez")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nombre_papa == "Oscar Rodriguez"


# ============================================================
# D.3 (Lily 2026-05-27): email_papa y telefono
# ============================================================


def test_aplicar_extraccion_email_nuevo() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(email_papa="oscar@example.com")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.email_papa == "oscar@example.com"


def test_aplicar_extraccion_email_no_sobrescribe() -> None:
    actual = EstadoCapturado(email_papa="ana@example.com")
    extr = ExtraccionTurno(email_papa="otro@example.com")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.email_papa == "ana@example.com"


def test_aplicar_extraccion_telefono_nuevo() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(telefono="8441234567")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.telefono == "8441234567"


def test_aplicar_extraccion_telefono_no_sobrescribe() -> None:
    actual = EstadoCapturado(telefono="8441234567")
    extr = ExtraccionTurno(telefono="9999999999")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.telefono == "8441234567"


def test_extractor_prompt_documenta_email_y_telefono() -> None:
    """El system prompt del extractor debe instruir cómo detectar email y celular."""
    from app.core.state_extractor import _SYSTEM_PROMPT

    p = _SYSTEM_PROMPT.lower()
    assert "email_papa" in p
    assert "telefono" in p
    assert "celular" in p
