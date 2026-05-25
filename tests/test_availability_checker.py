"""Tests del availability_checker (Bloque C.1 PASO 4).

Cubre:
- Funciones puras (cálculo de DOW, slot dentro/fuera de horario, colisión con citas)
- Generación de alternativas
- is_slot_available end-to-end con stubs de Supabase (respx)
"""

from __future__ import annotations

from datetime import datetime, time

import httpx
import pytest
import respx
from app.config import Settings
from app.core.appointment_extractor import TZ_MONTERREY
from app.tools.availability_checker import (
    LilyAvailabilityWindow,
    _dow_postgres_style,
    _generar_candidatos_alternativos,
    _slot_choca_con_citas,
    _slot_dentro_de_horario,
    is_slot_available,
)


def _settings() -> Settings:
    return Settings(
        env="test",
        supabase_url="https://x.supabase.co",
        supabase_service_key="srv-key",
    )


# Una ventana base: lunes a viernes 9:00-17:00, slots 60min.
def _ventanas_lun_vie() -> list[LilyAvailabilityWindow]:
    return [
        LilyAvailabilityWindow(
            day_of_week=d, start_time=time(9, 0), end_time=time(17, 0),
            slot_duration_minutes=60, active=True,
        )
        for d in (1, 2, 3, 4, 5)  # lun=1..vie=5 estilo PG
    ]


# ============================================================
# _dow_postgres_style
# ============================================================


def test_dow_lunes() -> None:
    # 2026-05-25 es lunes
    dt = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    assert _dow_postgres_style(dt) == 1


def test_dow_domingo() -> None:
    # 2026-05-24 es domingo
    dt = datetime(2026, 5, 24, tzinfo=TZ_MONTERREY)
    assert _dow_postgres_style(dt) == 0


def test_dow_sabado() -> None:
    dt = datetime(2026, 5, 30, tzinfo=TZ_MONTERREY)
    assert _dow_postgres_style(dt) == 6


# ============================================================
# _slot_dentro_de_horario
# ============================================================


def test_slot_dentro_de_horario_ok() -> None:
    """Lunes 10:00, 60min → dentro de 9-17."""
    dt = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is True


def test_slot_borde_inicio_ok() -> None:
    """Lunes 9:00, 60min → empieza exacto al inicio del horario."""
    dt = datetime(2026, 5, 25, 9, 0, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is True


def test_slot_borde_fin_ok() -> None:
    """Lunes 16:00, 60min → termina exacto a las 17:00 (incluido)."""
    dt = datetime(2026, 5, 25, 16, 0, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is True


def test_slot_se_pasa_del_fin() -> None:
    """Lunes 16:30, 60min → terminaría 17:30, fuera del horario."""
    dt = datetime(2026, 5, 25, 16, 30, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is False


def test_slot_antes_del_inicio() -> None:
    """Lunes 8:00 → antes del inicio."""
    dt = datetime(2026, 5, 25, 8, 0, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is False


def test_slot_domingo_no_laborable() -> None:
    """Domingo no tiene ventana activa."""
    dt = datetime(2026, 5, 24, 10, 0, tzinfo=TZ_MONTERREY)
    assert _slot_dentro_de_horario(dt, 60, _ventanas_lun_vie()) is False


# ============================================================
# _slot_choca_con_citas
# ============================================================


def test_choca_solapamiento_directo() -> None:
    """Slot 10:00-11:00 vs cita 10:30-11:30 → choca."""
    cita = datetime(2026, 5, 25, 10, 30, tzinfo=TZ_MONTERREY)
    nuevo = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    assert _slot_choca_con_citas(nuevo, 60, [(cita, 60)]) is True


def test_no_choca_si_termina_justo_antes() -> None:
    """Slot 9:00-10:00 vs cita 10:00-11:00 → NO choca (back-to-back)."""
    cita = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    nuevo = datetime(2026, 5, 25, 9, 0, tzinfo=TZ_MONTERREY)
    assert _slot_choca_con_citas(nuevo, 60, [(cita, 60)]) is False


def test_no_choca_si_empieza_justo_despues() -> None:
    """Slot 11:00-12:00 vs cita 10:00-11:00 → NO choca."""
    cita = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    nuevo = datetime(2026, 5, 25, 11, 0, tzinfo=TZ_MONTERREY)
    assert _slot_choca_con_citas(nuevo, 60, [(cita, 60)]) is False


def test_choca_con_segunda_cita_de_tres() -> None:
    citas = [
        (datetime(2026, 5, 25, 9, 0, tzinfo=TZ_MONTERREY), 60),
        (datetime(2026, 5, 25, 11, 30, tzinfo=TZ_MONTERREY), 60),
        (datetime(2026, 5, 25, 14, 0, tzinfo=TZ_MONTERREY), 60),
    ]
    nuevo = datetime(2026, 5, 25, 11, 0, tzinfo=TZ_MONTERREY)
    assert _slot_choca_con_citas(nuevo, 60, citas) is True


def test_no_choca_con_lista_vacia() -> None:
    nuevo = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    assert _slot_choca_con_citas(nuevo, 60, []) is False


# ============================================================
# _generar_candidatos_alternativos
# ============================================================


def test_genera_candidatos_dentro_del_horario() -> None:
    """Lunes 10:30 → genera slots cada 60min dentro de 9-17 del mismo día
    y siguientes laborables."""
    base = datetime(2026, 5, 25, 10, 30, tzinfo=TZ_MONTERREY)
    cands = _generar_candidatos_alternativos(base, 60, _ventanas_lun_vie())
    assert len(cands) > 0
    # Todos los candidatos caen dentro del horario de Lily
    for c in cands:
        assert _slot_dentro_de_horario(c, 60, _ventanas_lun_vie()) is True


def test_candidatos_ordenados_por_cercania() -> None:
    """El primer candidato debe ser el más cercano al horario solicitado."""
    base = datetime(2026, 5, 25, 10, 30, tzinfo=TZ_MONTERREY)
    cands = _generar_candidatos_alternativos(base, 60, _ventanas_lun_vie())
    # El más cercano debería ser 11:00 (siguiente slot mismo día)
    primer = cands[0]
    assert primer.day == 25
    assert primer.hour == 11


def test_candidatos_excluyen_dias_no_laborables() -> None:
    """Sábado y domingo no aparecen entre los candidatos."""
    base = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    cands = _generar_candidatos_alternativos(base, 60, _ventanas_lun_vie())
    for c in cands:
        dow = _dow_postgres_style(c)
        assert dow not in (0, 6), f"candidato {c} cae en fin de semana"


# ============================================================
# is_slot_available — end-to-end con respx
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_is_slot_available_ok() -> None:
    """Slot lunes 10:00 con horario 9-17 y sin citas → disponible."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": 1,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )

    fecha_hora = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)  # lunes
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)  # antes
    result = await is_slot_available(
        fecha_hora, duracion_minutos=60, settings=_settings(), now=now
    )
    assert result.available is True
    assert result.reason == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_is_slot_available_fuera_de_horario() -> None:
    """Lunes 18:30 → fuera de 9-17. Devuelve 3 alternativas."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )

    fecha_hora = datetime(2026, 5, 25, 18, 30, tzinfo=TZ_MONTERREY)  # lunes 18:30
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    assert result.reason == "fuera_de_horario"
    assert len(result.alternativas) == 3


@pytest.mark.asyncio
@respx.mock
async def test_is_slot_available_dia_no_laborable() -> None:
    """Sábado → día no laborable (solo lun-vie). Devuelve alternativas."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )

    fecha_hora = datetime(2026, 5, 30, 10, 0, tzinfo=TZ_MONTERREY)  # sábado
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    assert result.reason == "dia_no_laborable"
    assert len(result.alternativas) >= 1


@pytest.mark.asyncio
@respx.mock
async def test_is_slot_available_slot_ocupado() -> None:
    """Lunes 10:00 dentro de horario pero ya hay cita ahí → slot_ocupado."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    # Hay una cita a las 10:00 que dura 60min — solapa con el nuevo slot
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "fecha_hora": "2026-05-25T10:00:00-06:00",
                    "duracion_min": 60,
                    "status": "confirmada",
                }
            ],
        )
    )

    fecha_hora = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    assert result.reason == "slot_ocupado"


@pytest.mark.asyncio
async def test_is_slot_available_fecha_pasada() -> None:
    """Fecha pasada → fecha_pasada, sin consultar Supabase."""
    fecha_hora = datetime(2026, 5, 10, 10, 0, tzinfo=TZ_MONTERREY)
    now = datetime(2026, 5, 25, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    assert result.reason == "fecha_pasada"


@pytest.mark.asyncio
async def test_is_slot_available_sin_supabase() -> None:
    """Sin supabase_url → supabase_error sin lanzar."""
    settings_sin_supabase = Settings(env="test", supabase_url="")
    fecha_hora = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=settings_sin_supabase, now=now)
    assert result.available is False
    assert result.reason == "supabase_error"


@pytest.mark.asyncio
@respx.mock
async def test_is_slot_available_supabase_caido() -> None:
    """Supabase devuelve 500 → graceful fail."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(500, text="server error")
    )
    fecha_hora = datetime(2026, 5, 25, 10, 0, tzinfo=TZ_MONTERREY)
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    assert result.reason == "supabase_error"


@pytest.mark.asyncio
@respx.mock
async def test_alternativas_excluyen_slots_ocupados() -> None:
    """Las alternativas no incluyen slots que ya tienen cita."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": d,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
                for d in (1, 2, 3, 4, 5)
            ],
        )
    )
    # Hay citas a las 11:00 y 12:00 del lunes — las alternativas no las incluyen
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "fecha_hora": "2026-05-25T11:00:00-06:00",
                    "duracion_min": 60,
                    "status": "pendiente",
                },
                {
                    "fecha_hora": "2026-05-25T12:00:00-06:00",
                    "duracion_min": 60,
                    "status": "confirmada",
                },
            ],
        )
    )

    fecha_hora = datetime(2026, 5, 25, 18, 30, tzinfo=TZ_MONTERREY)  # fuera de horario
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_hora, settings=_settings(), now=now)
    assert result.available is False
    horas_propuestas = {(a.day, a.hour) for a in result.alternativas}
    # No deben proponer lunes 11 ni 12
    assert (25, 11) not in horas_propuestas
    assert (25, 12) not in horas_propuestas


@pytest.mark.asyncio
@respx.mock
async def test_normaliza_fecha_sin_tz() -> None:
    """fecha_hora sin tzinfo se asume Monterrey."""
    respx.get("https://x.supabase.co/rest/v1/lily_availability").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "day_of_week": 1,
                    "start_time": "09:00:00",
                    "end_time": "17:00:00",
                    "slot_duration_minutes": 60,
                    "active": True,
                }
            ],
        )
    )
    respx.get("https://x.supabase.co/rest/v1/appointments").mock(
        return_value=httpx.Response(200, json=[])
    )

    fecha_naive = datetime(2026, 5, 25, 10, 0)  # sin tz
    now = datetime(2026, 5, 20, tzinfo=TZ_MONTERREY)
    result = await is_slot_available(fecha_naive, settings=_settings(), now=now)
    assert result.available is True
