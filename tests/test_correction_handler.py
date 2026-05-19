"""Tests del correction_handler (Bloque 5.6 PASO 4)."""

from __future__ import annotations

import json

import pytest
from app.config import Settings
from app.core.correction_handler import (
    CorreccionDetectada,
    aplicar_correccion,
    detectar_correccion,
)
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo


def test_correccion_detectada_es_vacia_default() -> None:
    c = CorreccionDetectada()
    assert c.es_vacia is True


def test_correccion_detectada_no_vacia_con_nivel() -> None:
    c = CorreccionDetectada(nivel_buscado="kinder")
    assert c.es_vacia is False


def test_correccion_detectada_no_vacia_con_campos_a_limpiar() -> None:
    c = CorreccionDetectada(campos_a_limpiar=["nivel_buscado"])
    assert c.es_vacia is False


# ============================================================
# aplicar_correccion
# ============================================================


def test_aplicar_sobreescribe_nivel() -> None:
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL)
    correccion = CorreccionDetectada(nivel_buscado="kinder")
    nuevo = aplicar_correccion(estado, correccion)
    assert nuevo.nivel_buscado_actual == NivelEducativo.KINDER


def test_aplicar_limpia_nivel() -> None:
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL)
    correccion = CorreccionDetectada(campos_a_limpiar=["nivel_buscado"])
    nuevo = aplicar_correccion(estado, correccion)
    assert nuevo.nivel_buscado_actual is None


def test_aplicar_actualiza_edad_hijo() -> None:
    estado = EstadoCapturado(hijos=[HijoInfo(edad=5)])
    correccion = CorreccionDetectada(edad_hijo=7)
    nuevo = aplicar_correccion(estado, correccion)
    assert nuevo.hijos[0].edad == 7


def test_aplicar_crea_hijo_si_no_existe() -> None:
    estado = EstadoCapturado()
    correccion = CorreccionDetectada(nombre_hijo="Mateo", edad_hijo=8)
    nuevo = aplicar_correccion(estado, correccion)
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].nombre == "Mateo"
    assert nuevo.hijos[0].edad == 8


def test_aplicar_no_muta_estado_original() -> None:
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL)
    correccion = CorreccionDetectada(nivel_buscado="primaria")
    nuevo = aplicar_correccion(estado, correccion)
    assert estado.nivel_buscado_actual == NivelEducativo.MATERNAL  # sin cambio
    assert nuevo.nivel_buscado_actual == NivelEducativo.PRIMARIA


def test_aplicar_nivel_invalido_no_rompe() -> None:
    """Si el LLM devuelve un nivel inválido, se ignora silenciosamente."""
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL)
    correccion = CorreccionDetectada(nivel_buscado="superhero")  # no es un NivelEducativo válido
    nuevo = aplicar_correccion(estado, correccion)
    assert nuevo.nivel_buscado_actual == NivelEducativo.MATERNAL  # se mantuvo


def test_aplicar_actualiza_nombre_papa() -> None:
    estado = EstadoCapturado()
    correccion = CorreccionDetectada(nombre_papa="Juan")
    nuevo = aplicar_correccion(estado, correccion)
    assert nuevo.nombre_papa == "Juan"


# ============================================================
# detectar_correccion (con monkeypatch del LLM)
# ============================================================


@pytest.mark.asyncio
async def test_detectar_devuelve_none_sin_openai() -> None:
    """Sin openai_api_key, devuelve None graciosamente."""
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings()

        def is_configured(self) -> bool:
            return False

    openai_client._singleton = FakeOpenAI()
    try:
        r = await detectar_correccion("no, eso no era", EstadoCapturado())
        assert r is None
    finally:
        openai_client._singleton = None


@pytest.mark.asyncio
async def test_detectar_parsea_json_valido(monkeypatch) -> None:
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return json.dumps({"nivel_buscado": "kinder", "campos_a_limpiar": []})

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    r = await detectar_correccion(
        "No, no es maternal, es kinder",
        EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL),
    )
    assert r is not None
    assert r.nivel_buscado == "kinder"


@pytest.mark.asyncio
async def test_detectar_resilient_a_json_invalido(monkeypatch) -> None:
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return "no es json válido"

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    r = await detectar_correccion("no es maternal", EstadoCapturado())
    assert r is None  # no rompe


@pytest.mark.asyncio
async def test_detectar_devuelve_correccion_vacia_si_llm_no_detecta_nada(monkeypatch) -> None:
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return "{}"

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    r = await detectar_correccion("no me refería a eso", EstadoCapturado())
    assert r is not None
    assert r.es_vacia is True


@pytest.mark.asyncio
async def test_detectar_captura_instruccion_procedimental(monkeypatch) -> None:
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return json.dumps(
                {"instruccion_comportamiento": "no preguntes si está en otra escuela actualmente"}
            )

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    r = await detectar_correccion("No preguntes, está ahorita en alguna escuela", EstadoCapturado())
    assert r is not None
    assert r.instruccion_comportamiento is not None
    assert "no preguntes" in r.instruccion_comportamiento.lower()
