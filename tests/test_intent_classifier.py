"""Tests del intent_classifier."""

from __future__ import annotations

import pytest
from app.adapters.openai_client import OpenAIAdapter
from app.config import Settings
from app.core.intent_classifier import (
    Intent,
    _parse_result,
    classify_intent,
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
