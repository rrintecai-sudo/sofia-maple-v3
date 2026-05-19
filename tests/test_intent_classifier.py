"""Tests del intent_classifier."""

from __future__ import annotations

import pytest
from app.adapters.openai_client import OpenAIAdapter
from app.config import Settings
from app.core.intent_classifier import (
    Intent,
    _parse_result,
    classify_intent,
    es_respuesta_corta_al_turno_previo,
)


def test_parse_valid_json() -> None:
    raw = '{"intent": "pregunta_costos", "confidence": 0.92, "razonamiento_breve": "pregunta directa por precio"}'
    result = _parse_result(raw)
    assert result.intent == Intent.PREGUNTA_COSTOS
    assert result.confidence == 0.92


def test_parse_with_backticks() -> None:
    raw = '```json\n{"intent": "saludo_inicial", "confidence": 0.99}\n```'
    result = _parse_result(raw)
    assert result.intent == Intent.SALUDO_INICIAL


def test_parse_invalid_json_fallback() -> None:
    result = _parse_result("texto sin formato")
    assert result.intent == Intent.CONFUSO_OTRO
    assert result.confidence == 0.0


def test_parse_unknown_intent_value_fallback() -> None:
    raw = '{"intent": "intent_inventado", "confidence": 0.5}'
    result = _parse_result(raw)
    assert result.intent == Intent.CONFUSO_OTRO


def test_intent_result_confidence_bounds() -> None:
    # confidence > 1.0 debe fallar validación → pydantic ValidationError → fallback
    raw = '{"intent": "saludo_inicial", "confidence": 1.5}'
    result = _parse_result(raw)
    assert result.intent == Intent.CONFUSO_OTRO


@pytest.mark.asyncio
async def test_classify_returns_confuso_when_openai_not_configured(monkeypatch) -> None:
    """Sin OPENAI_API_KEY, classify_intent retorna confuso_otro sin lanzar."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    # También cachear el adapter singleton
    import app.adapters.openai_client as mod

    mod._singleton = OpenAIAdapter(settings=Settings(openai_api_key=""))
    result = await classify_intent("hola")
    assert result.intent == Intent.CONFUSO_OTRO
    assert result.confidence == 0.0
    # Limpiar
    mod._singleton = None
    get_settings.cache_clear()


# ============================================================
# es_respuesta_corta_al_turno_previo (Bloque 5.7 ATAQUE 2)
# ============================================================


def test_respuesta_corta_sin_turno_previo_es_false() -> None:
    """Guard A: sin turno previo del assistant, NO aplica."""
    assert es_respuesta_corta_al_turno_previo("sí", hay_turno_previo_assistant=False) is False
    assert es_respuesta_corta_al_turno_previo("5to", hay_turno_previo_assistant=False) is False


def test_respuesta_corta_si_simple() -> None:
    assert es_respuesta_corta_al_turno_previo("Sí", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_si_por_favor() -> None:
    assert (
        es_respuesta_corta_al_turno_previo("Si por favor", hay_turno_previo_assistant=True) is True
    )


def test_respuesta_corta_ok() -> None:
    assert es_respuesta_corta_al_turno_previo("ok", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_listo() -> None:
    assert es_respuesta_corta_al_turno_previo("Listo", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_claro() -> None:
    assert es_respuesta_corta_al_turno_previo("Claro", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_5to() -> None:
    assert es_respuesta_corta_al_turno_previo("5to", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_4to_primaria() -> None:
    assert (
        es_respuesta_corta_al_turno_previo("4to primaria", hay_turno_previo_assistant=True) is True
    )


def test_respuesta_corta_que_mas() -> None:
    assert es_respuesta_corta_al_turno_previo("que más?", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_cuentame() -> None:
    assert es_respuesta_corta_al_turno_previo("cuéntame", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_kinder() -> None:
    assert es_respuesta_corta_al_turno_previo("kinder", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_primaria() -> None:
    assert es_respuesta_corta_al_turno_previo("primaria", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_edad() -> None:
    assert es_respuesta_corta_al_turno_previo("9 años", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_aja() -> None:
    assert es_respuesta_corta_al_turno_previo("ajá", hay_turno_previo_assistant=True) is True


def test_respuesta_corta_negative_pregunta_larga() -> None:
    """Pregunta sustancial NO es respuesta corta."""
    assert (
        es_respuesta_corta_al_turno_previo(
            "¿Cómo manejan el bullying?", hay_turno_previo_assistant=True
        )
        is False
    )


def test_respuesta_corta_negative_mensaje_extenso() -> None:
    assert (
        es_respuesta_corta_al_turno_previo(
            "Estoy buscando algo distinto a lo tradicional", hay_turno_previo_assistant=True
        )
        is False
    )


def test_respuesta_corta_negative_palabra_random_corta() -> None:
    """'cuanto' no es respuesta corta confirmatoria — pregunta operativa."""
    assert es_respuesta_corta_al_turno_previo("cuanto", hay_turno_previo_assistant=True) is False


def test_respuesta_corta_negative_palabra_aislada_no_categorizada() -> None:
    assert es_respuesta_corta_al_turno_previo("xyz", hay_turno_previo_assistant=True) is False


def test_respuesta_corta_negative_demasiado_largo() -> None:
    """>15 chars descarta aunque sea confirmatorio."""
    assert (
        es_respuesta_corta_al_turno_previo("Sí claro por supuesto", hay_turno_previo_assistant=True)
        is False
    )


def test_respuesta_corta_vacio() -> None:
    assert es_respuesta_corta_al_turno_previo("", hay_turno_previo_assistant=True) is False
    assert es_respuesta_corta_al_turno_previo("   ", hay_turno_previo_assistant=True) is False


# ============================================================
# Guard saludo_inicial (hotfix post-5.7): si hay turno previo del assistant,
# el LLM NO puede clasificar como SALUDO_INICIAL — override a CONFUSO_OTRO.
# Bug capturado: "interactuara y que aprenda" → LLM marcaba saludo_inicial,
# Sofía se volvía a presentar a mitad de conversación.
# ============================================================


@pytest.mark.asyncio
async def test_guard_saludo_inicial_override_con_historial(monkeypatch) -> None:
    """LLM devuelve saludo_inicial pero hay_turno_previo_assistant=True →
    override a CONFUSO_OTRO."""
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return '{"intent": "saludo_inicial", "confidence": 0.85}'

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    result = await classify_intent("interactuara y que aprenda", hay_turno_previo_assistant=True)
    # Override a CONFUSO_OTRO — Sofía NO se vuelve a presentar
    assert result.intent == Intent.CONFUSO_OTRO
    assert result.confidence == 0.85  # preserva confidence original
    assert "override hotfix" in (result.razonamiento_breve or "")


@pytest.mark.asyncio
async def test_guard_saludo_inicial_sin_historial_no_override(monkeypatch) -> None:
    """LLM devuelve saludo_inicial Y hay_turno_previo_assistant=False →
    NO override. Primer turno legítimo."""
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return '{"intent": "saludo_inicial", "confidence": 0.95}'

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    result = await classify_intent("Hola", hay_turno_previo_assistant=False)
    assert result.intent == Intent.SALUDO_INICIAL  # sin override


@pytest.mark.asyncio
async def test_guard_no_aplica_a_otros_intents(monkeypatch) -> None:
    """Otros intents pasan sin tocar aunque hay_turno_previo_assistant=True."""
    from app.adapters import openai_client

    class FakeOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            return '{"intent": "pregunta_costos", "confidence": 0.9}'

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    result = await classify_intent("cuánto cuesta", hay_turno_previo_assistant=True)
    assert result.intent == Intent.PREGUNTA_COSTOS  # sin override
