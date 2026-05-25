"""Tests del extractor de fecha/hora para citas (Bloque C.1 PASO 3).

El extractor real usa gpt-4o-mini — aquí mockeamos el LLM y verificamos:
- Parsing de respuestas JSON válidas e inválidas
- Construcción del system prompt (fecha actual + día de la semana)
- to_datetime() en zona America/Monterrey
- Confidence threshold (< 0.7 = baja, no accionable)
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.adapters import openai_client
from app.config import Settings
from app.core.appointment_extractor import (
    CONFIDENCE_MIN,
    TZ_MONTERREY,
    AppointmentDateTime,
    _build_system_prompt,
    _parse_result,
    extract_datetime,
)


class _StubOpenAI:
    settings = Settings(openai_api_key="sk-test")

    def __init__(self, response: str) -> None:
        self._response = response

    def is_configured(self) -> bool:
        return True

    async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
        return self._response


# ============================================================
# _parse_result
# ============================================================


def test_parse_result_completo() -> None:
    raw = '{"fecha": "2026-05-26", "hora": "10:00", "confidence": 0.92, "razonamiento": "martes próximo"}'
    result = _parse_result(raw)
    assert result.fecha == "2026-05-26"
    assert result.hora == "10:00"
    assert result.confidence == 0.92
    assert result.es_completo is True
    assert result.es_alta_confianza is True


def test_parse_result_null_fecha() -> None:
    raw = '{"fecha": null, "hora": null, "confidence": 0.3, "razonamiento": "ambiguo"}'
    result = _parse_result(raw)
    assert result.fecha is None
    assert result.hora is None
    assert result.es_completo is False


def test_parse_result_con_backticks() -> None:
    raw = '```json\n{"fecha": "2026-06-01", "hora": "15:00", "confidence": 0.88, "razonamiento": "ok"}\n```'
    result = _parse_result(raw)
    assert result.fecha == "2026-06-01"
    assert result.hora == "15:00"


def test_parse_result_string_vacio_es_null() -> None:
    """fecha="" debe interpretarse como None, no como string."""
    raw = '{"fecha": "", "hora": "  ", "confidence": 0.4, "razonamiento": "x"}'
    result = _parse_result(raw)
    assert result.fecha is None
    assert result.hora is None


def test_parse_result_confidence_fuera_de_rango() -> None:
    """confidence > 1 se clamea a 1; < 0 a 0."""
    raw = '{"fecha": null, "hora": null, "confidence": 1.5, "razonamiento": "x"}'
    result = _parse_result(raw)
    assert result.confidence == 1.0

    raw2 = '{"fecha": null, "hora": null, "confidence": -0.2, "razonamiento": "x"}'
    result2 = _parse_result(raw2)
    assert result2.confidence == 0.0


def test_parse_result_json_invalido() -> None:
    result = _parse_result("no json aquí")
    assert result.fecha is None
    assert result.hora is None
    assert result.confidence == 0.0


def test_parse_result_confidence_no_numerico() -> None:
    raw = '{"fecha": "2026-05-26", "hora": "10:00", "confidence": "alto", "razonamiento": "x"}'
    result = _parse_result(raw)
    assert result.confidence == 0.0


# ============================================================
# to_datetime
# ============================================================


def test_to_datetime_completo() -> None:
    appt = AppointmentDateTime(fecha="2026-05-26", hora="10:00", confidence=0.9, razonamiento="x")
    dt = appt.to_datetime()
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 26
    assert dt.hour == 10
    assert dt.minute == 0
    assert dt.tzinfo == TZ_MONTERREY


def test_to_datetime_incompleto_devuelve_none() -> None:
    appt = AppointmentDateTime(fecha=None, hora="10:00", confidence=0.5, razonamiento="x")
    assert appt.to_datetime() is None


def test_to_datetime_formato_invalido() -> None:
    appt = AppointmentDateTime(fecha="2026/05/26", hora="10:00am", confidence=0.5, razonamiento="x")
    assert appt.to_datetime() is None


# ============================================================
# _build_system_prompt
# ============================================================


def test_build_system_prompt_incluye_fecha_y_dia() -> None:
    now = datetime(2026, 5, 25, 14, 30, tzinfo=TZ_MONTERREY)  # lunes
    prompt = _build_system_prompt(now)
    assert "2026-05-25" in prompt
    assert "lunes" in prompt
    assert "America/Monterrey" in prompt


def test_build_system_prompt_dias_semana() -> None:
    fechas_dias = [
        (datetime(2026, 5, 25, tzinfo=TZ_MONTERREY), "lunes"),
        (datetime(2026, 5, 26, tzinfo=TZ_MONTERREY), "martes"),
        (datetime(2026, 5, 27, tzinfo=TZ_MONTERREY), "miércoles"),
        (datetime(2026, 5, 28, tzinfo=TZ_MONTERREY), "jueves"),
        (datetime(2026, 5, 29, tzinfo=TZ_MONTERREY), "viernes"),
        (datetime(2026, 5, 30, tzinfo=TZ_MONTERREY), "sábado"),
        (datetime(2026, 5, 31, tzinfo=TZ_MONTERREY), "domingo"),
    ]
    for fecha, dia in fechas_dias:
        prompt = _build_system_prompt(fecha)
        assert dia in prompt, f"{fecha.date()} debería ser {dia}"


def test_build_system_prompt_pide_no_inventar() -> None:
    """Anti-alucinación: el prompt prohíbe inventar fechas."""
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    prompt = _build_system_prompt(now)
    assert "NUNCA inventes" in prompt
    assert "futuras" in prompt.lower()


# ============================================================
# extract_datetime (end-to-end con stub)
# ============================================================


@pytest.mark.asyncio
async def test_extract_datetime_caso_martes_10am(monkeypatch) -> None:
    """'el martes 10am' → próximo martes a las 10:00."""
    monkeypatch.setattr(
        openai_client,
        "_singleton",
        _StubOpenAI(
            '{"fecha": "2026-05-26", "hora": "10:00", "confidence": 0.92, "razonamiento": "próximo martes"}'
        ),
    )
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)  # lunes
    result = await extract_datetime("el martes 10am", now=now)
    assert result.fecha == "2026-05-26"
    assert result.hora == "10:00"
    assert result.es_alta_confianza is True


@pytest.mark.asyncio
async def test_extract_datetime_manana_3pm(monkeypatch) -> None:
    """'mañana a las 3' → +1 día, 15:00."""
    monkeypatch.setattr(
        openai_client,
        "_singleton",
        _StubOpenAI(
            '{"fecha": "2026-05-26", "hora": "15:00", "confidence": 0.9, "razonamiento": "mañana 3 PM"}'
        ),
    )
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    result = await extract_datetime("mañana a las 3", now=now)
    assert result.fecha == "2026-05-26"
    assert result.hora == "15:00"


@pytest.mark.asyncio
async def test_extract_datetime_cualquier_dia_es_null(monkeypatch) -> None:
    """'cualquier día' → fecha=null, confidence baja."""
    monkeypatch.setattr(
        openai_client,
        "_singleton",
        _StubOpenAI('{"fecha": null, "hora": null, "confidence": 0.2, "razonamiento": "ambiguo"}'),
    )
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    result = await extract_datetime("cualquier día", now=now)
    assert result.fecha is None
    assert result.hora is None
    assert result.es_alta_confianza is False


@pytest.mark.asyncio
async def test_extract_datetime_proxima_semana_es_null(monkeypatch) -> None:
    """'la próxima semana' sin día específico → ambiguo, null."""
    monkeypatch.setattr(
        openai_client,
        "_singleton",
        _StubOpenAI(
            '{"fecha": null, "hora": null, "confidence": 0.35, "razonamiento": "sin día específico"}'
        ),
    )
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    result = await extract_datetime("la próxima semana", now=now)
    assert result.es_completo is False


@pytest.mark.asyncio
async def test_extract_datetime_sin_api_key(monkeypatch) -> None:
    """Sin OPENAI_API_KEY no levanta excepción — retorna confidence 0."""
    from app.adapters.openai_client import OpenAIAdapter

    monkeypatch.setattr(
        openai_client, "_singleton", OpenAIAdapter(settings=Settings(openai_api_key=""))
    )
    result = await extract_datetime("mañana a las 10")
    assert result.fecha is None
    assert result.confidence == 0.0
    assert "not configured" in result.razonamiento.lower()


@pytest.mark.asyncio
async def test_extract_datetime_normaliza_now_sin_tz(monkeypatch) -> None:
    """Si `now` viene sin tzinfo, lo normaliza a America/Monterrey."""
    monkeypatch.setattr(
        openai_client,
        "_singleton",
        _StubOpenAI(
            '{"fecha": "2026-05-26", "hora": "10:00", "confidence": 0.9, "razonamiento": "ok"}'
        ),
    )
    now_naive = datetime(2026, 5, 25, 12, 0)  # sin tzinfo
    result = await extract_datetime("el martes 10am", now=now_naive)
    assert result.fecha == "2026-05-26"


@pytest.mark.asyncio
async def test_extract_datetime_api_error(monkeypatch) -> None:
    """Excepción del LLM → fallback graceful."""

    class FailingOpenAI:
        settings = Settings(openai_api_key="sk-test")

        def is_configured(self) -> bool:
            return True

        async def classify(self, text: str, instructions: str, model: str | None = None) -> str:
            raise RuntimeError("API down")

    monkeypatch.setattr(openai_client, "_singleton", FailingOpenAI())
    result = await extract_datetime("el martes 10am")
    assert result.fecha is None
    assert result.razonamiento == "api_error"


# ============================================================
# Confidence threshold
# ============================================================


def test_confidence_min_es_07() -> None:
    """CONFIDENCE_MIN documentado en el módulo para que el orchestrator
    use el mismo umbral."""
    assert CONFIDENCE_MIN == 0.7


def test_es_alta_confianza_borde() -> None:
    """Justo en 0.7 es alta confianza; 0.69 no."""
    appt_alta = AppointmentDateTime(
        fecha="2026-05-26", hora="10:00", confidence=0.7, razonamiento=""
    )
    appt_baja = AppointmentDateTime(
        fecha="2026-05-26", hora="10:00", confidence=0.69, razonamiento=""
    )
    assert appt_alta.es_alta_confianza is True
    assert appt_baja.es_alta_confianza is False


def test_tz_monterrey_correcta() -> None:
    """Sanity: el módulo apunta a la TZ que se usa en producción."""
    assert TZ_MONTERREY == ZoneInfo("America/Monterrey")
