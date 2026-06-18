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
    es_confirmacion,
    extract_datetime,
    extraer_fecha_explicita,
    extraer_fecha_relativa,
    extraer_hora_de_numero_suelto,
    extraer_hora_simple,
    extraer_proximo_dia_semana,
)


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("10", "10:00"),
        ("8", "08:00"),
        ("12", "12:00"),
        ("1", "13:00"),  # 1-7 = PM por el horario de Lily
        ("3", "15:00"),
        ("13", "13:00"),
        ("10:30", "10:30"),
        ("a las 11", "11:00"),
        ("11hrs", "11:00"),
        # No es un número suelto → None (lo maneja extraer_hora_simple u otro path).
        ("esta bien", None),
        ("10 años", None),
        ("", None),
    ],
)
def test_extraer_hora_de_numero_suelto(texto, esperado) -> None:
    assert extraer_hora_de_numero_suelto(texto) == esperado


# ============================================================
# FIX (a) 2026-06-01 — hora "sucia": 10a, 10hrs, etc.
# ============================================================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("viernes 10a,", "10:00"),  # typo real de Oscar
        ("10a", "10:00"),
        ("2p", "14:00"),
        ("10hrs", "10:00"),
        ("10 hrs", "10:00"),
        ("14h", "14:00"),
        ("10 horas", "10:00"),
    ],
)
def test_extraer_hora_simple_sucia(texto, esperado) -> None:
    assert extraer_hora_simple(texto) == esperado


# ============================================================
# FIX (b) 2026-06-01 — es_confirmacion + extraer_fecha_explicita
# ============================================================


@pytest.mark.parametrize(
    "texto",
    [
        "sí",
        "si",
        "si dale",
        "sí dale",
        "dale",
        "ok",
        "okey",
        "va",
        "correcto",
        "exacto",
        "claro",
        "perfecto",
        "de acuerdo",
        "está bien",
        "si esta bien",
        "así es",
    ],
)
def test_es_confirmacion_positivos(texto) -> None:
    assert es_confirmacion(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "no",
        "sí pero el lunes",
        "mejor el martes",
        "quiero kinder",
        "no gracias",
        "a qué hora",
        "cuánto cuesta",
    ],
)
def test_es_confirmacion_negativos(texto) -> None:
    assert es_confirmacion(texto) is False


def test_extraer_fecha_explicita_dia_mes() -> None:
    now = datetime(2026, 6, 1, tzinfo=TZ_MONTERREY)  # lunes 1 jun
    assert extraer_fecha_explicita("viernes 5 de junio", now) == "2026-06-05"
    assert extraer_fecha_explicita("el 5 de junio a las 10", now) == "2026-06-05"


def test_extraer_fecha_explicita_mes_pasado_va_a_proximo_anio() -> None:
    now = datetime(2026, 6, 1, tzinfo=TZ_MONTERREY)
    # enero ya pasó este año → próximo enero
    assert extraer_fecha_explicita("15 de enero", now) == "2027-01-15"


def test_extraer_fecha_explicita_sin_fecha() -> None:
    now = datetime(2026, 6, 1, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_explicita("sí dale", now) is None
    assert extraer_fecha_explicita("a las 10", now) is None


# ============================================================
# extraer_hora_simple (FIX 2026-06-01 — hora suelta determinística)
# ============================================================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("2pm", "14:00"),
        ("2 pm", "14:00"),
        ("2 p.m.", "14:00"),
        ("2:30pm", "14:30"),
        ("10am", "10:00"),
        ("10 a.m.", "10:00"),
        ("12pm", "12:00"),
        ("12am", "00:00"),
        ("14:00", "14:00"),
        ("9:15", "09:15"),
        ("a las 2", "14:00"),
        ("a las 10", "10:00"),
        ("a las 2 pm", "14:00"),
        ("2 de la tarde", "14:00"),
        ("9 de la mañana", "09:00"),
        ("8 de la noche", "20:00"),
    ],
)
def test_extraer_hora_simple_positivos(texto, esperado) -> None:
    assert extraer_hora_simple(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["tengo 4 años", "kinder 2", "mi hijo de 5", "2 kinder", "somos 3", "hola", ""],
)
def test_extraer_hora_simple_no_falsos_positivos(texto) -> None:
    """No debe confundir edades/grados/conteos con una hora."""
    assert extraer_hora_simple(texto) is None


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
    assert "futur" in prompt.lower()


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


# ============================================================
# FIX (2026-06-02) — día de semana suelto → próxima ocurrencia DETERMINÍSTICA
# (el LLM no debe ser load-bearing para "el viernes")
# ============================================================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("el viernes 10am", "2026-06-05"),  # lunes 1 → próximo viernes 5
        ("nos vemos el viernes", "2026-06-05"),
        ("viernes", "2026-06-05"),
        ("este jueves", "2026-06-04"),
        ("próximo miércoles", "2026-06-03"),
        ("el martes", "2026-06-02"),  # mañana
        ("puedo el lunes", "2026-06-01"),  # HOY (lunes, antes de las 15:00) → hoy
    ],
)
def test_extraer_proximo_dia_semana_desde_lunes(texto, esperado) -> None:
    now = datetime(2026, 6, 1, 9, 0, tzinfo=TZ_MONTERREY)  # lunes 1 jun, 09:00
    assert extraer_proximo_dia_semana(texto, now) == esperado


def test_extraer_proximo_dia_semana_hoy_ya_cerro_va_a_proxima_semana() -> None:
    # viernes 5 jun a las 16:00 (ya pasó el horario de atención) → "el viernes"
    # NO es hoy, es el de la próxima semana.
    now = datetime(2026, 6, 5, 16, 0, tzinfo=TZ_MONTERREY)
    assert extraer_proximo_dia_semana("el viernes", now) == "2026-06-12"


def test_extraer_proximo_dia_semana_hoy_aun_abierto_es_hoy() -> None:
    # viernes 5 jun a las 09:00 (aún dentro del horario) → "el viernes" = hoy.
    now = datetime(2026, 6, 5, 9, 0, tzinfo=TZ_MONTERREY)
    assert extraer_proximo_dia_semana("el viernes", now) == "2026-06-05"


def test_extraer_proximo_dia_semana_sin_dia() -> None:
    now = datetime(2026, 6, 1, 9, 0, tzinfo=TZ_MONTERREY)
    assert extraer_proximo_dia_semana("tiene 4 años", now) is None
    assert extraer_proximo_dia_semana("a las 10am", now) is None


# ============================================================
# FIX (2026-06-04) — fechas relativas: "hoy" / "mañana" (antes se repetía la
# pregunta del día sin resolver). Cierre de Lily = 15:00 (8 a.m.–3 p.m.).
# ============================================================


def test_fecha_relativa_hoy_antes_del_cierre() -> None:
    # miércoles 3-jun 9:00 → "hoy" = hoy mismo (3-jun).
    now = datetime(2026, 6, 3, 9, 0, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_relativa("hoy puedo", now) == "2026-06-03"


def test_fecha_relativa_hoy_despues_del_cierre_va_a_proximo_dia() -> None:
    # miércoles 3-jun 16:00 (ya cerró Lily) → "hoy" ofrece el próximo día hábil (jue 4).
    now = datetime(2026, 6, 3, 16, 0, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_relativa("hoy", now) == "2026-06-04"


def test_fecha_relativa_hoy_viernes_tarde_salta_finde() -> None:
    # viernes 5-jun 16:00 → "hoy" tras el cierre → próximo hábil = lunes 8-jun (no sáb/dom).
    now = datetime(2026, 6, 5, 16, 0, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_relativa("hoy", now) == "2026-06-08"


def test_fecha_relativa_manana_y_pasado() -> None:
    now = datetime(2026, 6, 3, 9, 0, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_relativa("mañana a las 10", now) == "2026-06-04"
    assert extraer_fecha_relativa("pasado mañana", now) == "2026-06-05"


def test_fecha_relativa_sin_relativa() -> None:
    now = datetime(2026, 6, 3, 9, 0, tzinfo=TZ_MONTERREY)
    assert extraer_fecha_relativa("el viernes", now) is None
    assert extraer_fecha_relativa("a las 10am", now) is None


def test_motivo_ajuste_fecha_relativa_hoy_cerrado() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.core.appointment_extractor import (
        extraer_fecha_relativa,
        motivo_ajuste_fecha_relativa,
    )

    mty = ZoneInfo("America/Monterrey")
    tarde = datetime(2026, 6, 10, 15, 0, tzinfo=mty)  # miércoles 3 p.m.
    # 'hoy' se mueve a jueves 11 y la razón se explica.
    assert extraer_fecha_relativa("hoy", tarde) == "2026-06-11"
    assert "cerramos" in (motivo_ajuste_fecha_relativa("hoy", tarde) or "").lower()
    # 'hoy' temprano NO se mueve → sin razón.
    manana = datetime(2026, 6, 10, 9, 0, tzinfo=mty)
    assert extraer_fecha_relativa("hoy", manana) == "2026-06-10"
    assert motivo_ajuste_fecha_relativa("hoy", manana) is None
