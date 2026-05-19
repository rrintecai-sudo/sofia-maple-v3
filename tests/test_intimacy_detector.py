"""Tests del detector de momentos íntimos (Bloque 5.6 PASO 3)."""

from __future__ import annotations

import pytest
from app.core.intimacy_detector import (
    IntimacyResult,
    detectar_intimidad,
    detectar_intimidad_async,
)
from app.core.state import EstadoConversacion, FaseJourney


def _estado(fase: FaseJourney = FaseJourney.DESCUBRIMIENTO) -> EstadoConversacion:
    e = EstadoConversacion.nueva("web:test")
    e.fase_journey = fase
    return e


def test_intimo_operativo_es_falso() -> None:
    r = detectar_intimidad("¿Cuánto cuesta la colegiatura?", _estado())
    assert r.es_intimo is False
    assert r.confianza > 0.9
    assert "operativ" in r.razon.lower()


def test_intimo_horarios_no_es_intimo() -> None:
    r = detectar_intimidad("¿Cuál es el horario de primaria?", _estado())
    assert r.es_intimo is False


def test_intimo_con_keyword_miedo() -> None:
    r = detectar_intimidad("Mi miedo principal es que se aísle como yo cuando era niño", _estado())
    assert r.es_intimo is True
    assert r.confianza >= 0.85


def test_intimo_con_diagnostico() -> None:
    r = detectar_intimidad(
        "Mi hijo tiene diagnóstico de TDAH y nos preocupa cómo lo van a tratar",
        _estado(),
    )
    assert r.es_intimo is True


def test_intimo_con_narrativa_personal() -> None:
    r = detectar_intimidad("Yo crecí sin que nadie me escuchara, no quiero eso", _estado())
    assert r.es_intimo is True


def test_intimo_short_followup_tras_emocional() -> None:
    """'Sí' tras un mensaje previo emocional → íntimo."""
    r = detectar_intimidad(
        "Sí",
        _estado(),
        historial_papa=["Me preocupa que sea muy tímido como yo era"],
    )
    assert r.es_intimo is True
    assert "followup" in r.razon.lower()


def test_intimo_short_followup_sin_contexto_descubrimiento() -> None:
    """En fase descubrimiento, short followup se trata como íntimo (con conf media)."""
    r = detectar_intimidad("Sí", _estado(FaseJourney.DESCUBRIMIENTO))
    assert r.es_intimo is True
    assert 0.5 <= r.confianza < 0.7


def test_intimo_short_followup_en_otra_fase() -> None:
    """'Sí' en fase información (no descubrimiento) → NO íntimo por default."""
    r = detectar_intimidad("Sí", _estado(FaseJourney.INFORMACION))
    assert r.es_intimo is False


def test_intimo_mensaje_largo_en_descubrimiento() -> None:
    r = detectar_intimidad(
        "Estoy buscando algo distinto a las escuelas tradicionales para que mi peque pueda crecer feliz",
        _estado(FaseJourney.DESCUBRIMIENTO),
    )
    assert r.es_intimo is True


def test_intimo_mensaje_corto_no_emocional() -> None:
    """'Hola' es saludo neutro — NO íntimo."""
    r = detectar_intimidad("Hola", _estado(FaseJourney.BIENVENIDA))
    assert r.es_intimo is False


def test_intimo_que_mas_en_descubrimiento() -> None:
    """'Qué más' en descubrimiento es short followup íntimo."""
    r = detectar_intimidad("Qué más?", _estado(FaseJourney.DESCUBRIMIENTO))
    assert r.es_intimo is True


def test_intimacy_result_dataclass_frozen() -> None:
    r = IntimacyResult(es_intimo=True, razon="test", confianza=0.8)
    assert r.es_intimo is True
    with pytest.raises(AttributeError):
        r.es_intimo = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_async_no_llama_llm_si_confianza_alta() -> None:
    """Heurística devuelve confianza 0.95 (operativo) → no toca LLM aunque
    use_llm_fallback=True."""
    r = await detectar_intimidad_async(
        "¿Cuánto cuesta primaria?",
        _estado(),
        use_llm_fallback=True,
    )
    assert r.es_intimo is False
    assert "LLM" not in r.razon  # no se llamó


@pytest.mark.asyncio
async def test_async_fallback_graceful_si_llm_no_configurado(monkeypatch) -> None:
    """Si OpenAI no está configurado, devuelve resultado heurístico."""
    from app.adapters import openai_client
    from app.config import Settings

    class FakeOpenAI:
        settings = Settings()

        def is_configured(self) -> bool:
            return False

        async def classify(self, *args, **kwargs):
            raise RuntimeError("should not be called")

    monkeypatch.setattr(openai_client, "_singleton", FakeOpenAI())
    r = await detectar_intimidad_async(
        "Sí",
        _estado(FaseJourney.INFORMACION),  # heurística → no íntimo, conf 0.4
        use_llm_fallback=True,
    )
    # No falla aunque el LLM no esté disponible
    assert r.es_intimo is False
    monkeypatch.setattr(openai_client, "_singleton", None)
