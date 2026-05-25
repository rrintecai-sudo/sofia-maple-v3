"""Verificador de disponibilidad para agendar citas con Lily (Bloque C.1 PASO 4).

Consulta dos tablas:
- `lily_availability` — horarios laborales de Lily (editables por ella)
- `appointments` — citas ya agendadas con status pendiente/confirmada

Si el slot solicitado no está disponible, propone 3 alternativas cercanas
(mismo día otras horas, siguiente día laboral misma hora) que SÍ caen
dentro del horario de Lily y NO están ocupadas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Literal

import httpx

from app.config import Settings, get_settings
from app.core.appointment_extractor import TZ_MONTERREY

log = logging.getLogger(__name__)

ReasonCode = Literal[
    "ok",
    "fuera_de_horario",
    "slot_ocupado",
    "dia_no_laborable",
    "fecha_pasada",
    "supabase_error",
]

# Status de appointments que bloquean un slot (cuentan como "ocupado").
BLOCKING_STATUSES = ("pendiente", "confirmada")


@dataclass
class LilyAvailabilityWindow:
    """Una fila de `lily_availability` con el horario activo de Lily un día."""

    day_of_week: int  # 0=domingo..6=sábado (postgres style: DOW de PG funcs)
    start_time: time
    end_time: time
    slot_duration_minutes: int
    active: bool


@dataclass
class AvailabilityResult:
    available: bool
    reason: ReasonCode
    alternativas: list[datetime] = field(default_factory=list)
    mensaje: str = ""


# ============================================================
# Helpers de fecha
# ============================================================


def _dow_postgres_style(dt: datetime) -> int:
    """day_of_week tal como lo usamos en lily_availability:
    0=domingo, 1=lunes, ..., 6=sábado (mismo orden que PG EXTRACT(DOW)).
    """
    # weekday() en Python: 0=lunes, 6=domingo
    # Conversión: (weekday()+1) % 7 → 0=domingo, 1=lunes, ..., 6=sábado
    return (dt.weekday() + 1) % 7


def _to_monterrey(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_MONTERREY)
    return dt.astimezone(TZ_MONTERREY)


def _parse_time_str(s: str) -> time:
    """Acepta 'HH:MM' o 'HH:MM:SS'."""
    parts = s.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return time(h, m)


def _parse_appt_ts(ts: str) -> datetime:
    """Convierte el timestamp ISO devuelto por Supabase a datetime tz-aware
    en America/Monterrey."""
    # Supabase devuelve "2026-05-26T16:00:00+00:00" o sin "+" (UTC implícito)
    s = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Si llega sin tz, asumimos UTC (PostgREST suele incluirlo, pero por seguridad)
        from zoneinfo import ZoneInfo

        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(TZ_MONTERREY)


# ============================================================
# Consultas a Supabase
# ============================================================


async def _fetch_availability_windows(
    settings: Settings,
) -> list[LilyAvailabilityWindow]:
    """Trae todas las ventanas activas de lily_availability."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/lily_availability",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "active": "eq.true",
                    "select": "day_of_week,start_time,end_time,slot_duration_minutes,active",
                    "order": "day_of_week.asc,start_time.asc",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("fetch_availability_windows failed", extra={"error": str(exc)})
        return []

    return [
        LilyAvailabilityWindow(
            day_of_week=int(r["day_of_week"]),
            start_time=_parse_time_str(r["start_time"]),
            end_time=_parse_time_str(r["end_time"]),
            slot_duration_minutes=int(r.get("slot_duration_minutes") or 60),
            active=bool(r.get("active", True)),
        )
        for r in rows
    ]


async def _fetch_appointments_in_range(
    settings: Settings, start: datetime, end: datetime
) -> list[tuple[datetime, int]]:
    """Trae citas (status pendiente/confirmada) cuyo inicio cae en [start, end).

    Devuelve lista de (fecha_hora_local, duracion_min).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/appointments",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={
                    "status": f"in.({','.join(BLOCKING_STATUSES)})",
                    "fecha_hora": [
                        f"gte.{start.astimezone(TZ_MONTERREY).isoformat()}",
                        f"lt.{end.astimezone(TZ_MONTERREY).isoformat()}",
                    ],
                    "select": "fecha_hora,duracion_min,status",
                    "order": "fecha_hora.asc",
                },
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning(
            "fetch_appointments_in_range failed",
            extra={"error": str(exc), "start": str(start), "end": str(end)},
        )
        return []

    return [(_parse_appt_ts(r["fecha_hora"]), int(r.get("duracion_min") or 60)) for r in rows]


# ============================================================
# Lógica de slot
# ============================================================


def _slot_dentro_de_horario(
    dt: datetime, duracion_min: int, windows: list[LilyAvailabilityWindow]
) -> bool:
    """¿El slot [dt, dt+duracion) cae completamente dentro de alguna ventana activa
    para ese día de la semana?"""
    dow = _dow_postgres_style(dt)
    end_dt = dt + timedelta(minutes=duracion_min)
    for w in windows:
        if w.day_of_week != dow or not w.active:
            continue
        win_start = dt.replace(
            hour=w.start_time.hour, minute=w.start_time.minute, second=0, microsecond=0
        )
        win_end = dt.replace(
            hour=w.end_time.hour, minute=w.end_time.minute, second=0, microsecond=0
        )
        if win_start <= dt and end_dt <= win_end:
            return True
    return False


def _slot_choca_con_citas(
    dt: datetime, duracion_min: int, citas: list[tuple[datetime, int]]
) -> bool:
    """¿Hay solape entre [dt, dt+duracion) y alguna cita existente?"""
    end_dt = dt + timedelta(minutes=duracion_min)
    for cita_inicio, cita_dur in citas:
        cita_fin = cita_inicio + timedelta(minutes=cita_dur)
        # Solape si NO termina antes Y NO empieza después
        if dt < cita_fin and cita_inicio < end_dt:
            return True
    return False


def _dia_es_laborable(dt: datetime, windows: list[LilyAvailabilityWindow]) -> bool:
    """¿El día de la semana tiene al menos una ventana activa?"""
    dow = _dow_postgres_style(dt)
    return any(w.day_of_week == dow and w.active for w in windows)


def _generar_candidatos_alternativos(
    fecha_hora_solicitada: datetime,
    duracion_min: int,
    windows: list[LilyAvailabilityWindow],
    max_dias_adelante: int = 7,
) -> list[datetime]:
    """Genera slots candidatos cercanos al horario solicitado, dentro de
    `max_dias_adelante` días. NO verifica colisión con citas — eso lo hace
    el caller. Solo aplica la regla de horario de Lily.

    Estrategia (en orden de cercanía):
      1. Mismo día, mismas/siguientes horas dentro del horario
      2. Siguientes días laborables, misma hora (o más cercana válida)
    """
    candidatos: list[datetime] = []
    base = fecha_hora_solicitada
    for offset_dias in range(0, max_dias_adelante + 1):
        dia = base + timedelta(days=offset_dias)
        dow = _dow_postgres_style(dia)
        for w in windows:
            if w.day_of_week != dow or not w.active:
                continue
            # Generar slots de slot_duration_minutes desde start_time hasta end_time
            slot_min = w.slot_duration_minutes
            current = dia.replace(
                hour=w.start_time.hour, minute=w.start_time.minute, second=0, microsecond=0
            )
            win_end_dt = dia.replace(
                hour=w.end_time.hour, minute=w.end_time.minute, second=0, microsecond=0
            )
            while current + timedelta(minutes=duracion_min) <= win_end_dt:
                if current > base and current != fecha_hora_solicitada:
                    candidatos.append(current)
                current = current + timedelta(minutes=slot_min)
    # Ordenar por proximidad al horario solicitado
    candidatos.sort(key=lambda c: abs((c - fecha_hora_solicitada).total_seconds()))
    return candidatos


# ============================================================
# API pública
# ============================================================


async def is_slot_available(
    fecha_hora: datetime,
    *,
    duracion_minutos: int = 60,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> AvailabilityResult:
    """Verifica si el slot solicitado está disponible y, si no, propone hasta
    3 alternativas cercanas.

    Args:
        fecha_hora: cuando el papá quiere visitar (tz-aware preferido, asume
            America/Monterrey si naive).
        duracion_minutos: duración del slot (default 60).
        settings: opcional, inyectable para tests.
        now: opcional, datetime actual para verificación de "fecha pasada"
            en tests determinísticos. Default = datetime.now(TZ_MONTERREY).
    """
    settings = settings or get_settings()
    fecha_hora = _to_monterrey(fecha_hora)
    now_local = _to_monterrey(now or datetime.now(TZ_MONTERREY))

    # Sanity: no agendar en el pasado
    if fecha_hora <= now_local:
        return AvailabilityResult(
            available=False,
            reason="fecha_pasada",
            alternativas=[],
            mensaje="La fecha solicitada ya pasó. ¿Te queda alguna fecha próxima?",
        )

    if not settings.supabase_url:
        return AvailabilityResult(
            available=False,
            reason="supabase_error",
            mensaje="No pude verificar disponibilidad ahora. Inténtalo de nuevo.",
        )

    windows = await _fetch_availability_windows(settings)
    if not windows:
        return AvailabilityResult(
            available=False,
            reason="supabase_error",
            mensaje="No pude verificar disponibilidad ahora. Inténtalo de nuevo.",
        )

    # ¿Día laborable de Lily?
    if not _dia_es_laborable(fecha_hora, windows):
        alts = await _proponer_alternativas(fecha_hora, duracion_minutos, windows, settings)
        return AvailabilityResult(
            available=False,
            reason="dia_no_laborable",
            alternativas=alts,
            mensaje="Ese día Lily no está disponible.",
        )

    # ¿Dentro del horario?
    if not _slot_dentro_de_horario(fecha_hora, duracion_minutos, windows):
        alts = await _proponer_alternativas(fecha_hora, duracion_minutos, windows, settings)
        return AvailabilityResult(
            available=False,
            reason="fuera_de_horario",
            alternativas=alts,
            mensaje="Ese horario está fuera del rango de Lily.",
        )

    # ¿Choca con otra cita?
    rango_inicio = fecha_hora - timedelta(hours=2)
    rango_fin = fecha_hora + timedelta(hours=2)
    citas = await _fetch_appointments_in_range(settings, rango_inicio, rango_fin)
    if _slot_choca_con_citas(fecha_hora, duracion_minutos, citas):
        alts = await _proponer_alternativas(fecha_hora, duracion_minutos, windows, settings)
        return AvailabilityResult(
            available=False,
            reason="slot_ocupado",
            alternativas=alts,
            mensaje="Esa hora ya está ocupada.",
        )

    return AvailabilityResult(available=True, reason="ok", mensaje="Slot disponible.")


async def _proponer_alternativas(
    fecha_hora: datetime,
    duracion_min: int,
    windows: list[LilyAvailabilityWindow],
    settings: Settings,
    n: int = 3,
) -> list[datetime]:
    """Genera 3 alternativas próximas que pasan horario Y no chocan con citas."""
    candidatos = _generar_candidatos_alternativos(fecha_hora, duracion_min, windows)
    if not candidatos:
        return []

    # Trae citas en una ventana amplia (7 días desde el primer candidato)
    rango_inicio = min(candidatos[0], fecha_hora) - timedelta(hours=1)
    rango_fin = max(candidatos[-1] if candidatos else fecha_hora, fecha_hora) + timedelta(days=1)
    citas = await _fetch_appointments_in_range(settings, rango_inicio, rango_fin)

    seleccionadas: list[datetime] = []
    for cand in candidatos:
        if not _slot_choca_con_citas(cand, duracion_min, citas):
            seleccionadas.append(cand)
            if len(seleccionadas) >= n:
                break
    return seleccionadas
