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
    resumen: str = ""  # resumen humano del horario real de Lily (para el prompt)


_DOW_NOMBRES = {
    0: "domingo",
    1: "lunes",
    2: "martes",
    3: "miércoles",
    4: "jueves",
    5: "viernes",
    6: "sábado",
}
# Orden semanal lunes→domingo para listar rangos contiguos.
_DOW_ORDEN = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}


def _fmt_hhmm(t: time) -> str:
    return f"{t.hour}:{t.minute:02d}"


def _fmt_hora_humana(t: time) -> str:
    """'08:00' → '8:00 a.m.'; '15:00' → '3:00 p.m.' (estilo local mexicano)."""
    sufijo = "a.m." if t.hour < 12 else "p.m."
    h12 = t.hour % 12 or 12
    return f"{h12}:{t.minute:02d} {sufijo}"


def resumen_disponibilidad_humano(windows: list[LilyAvailabilityWindow]) -> str:
    """'lunes a viernes de 8:00 a.m. a 3:00 p.m.' a partir de las ventanas activas.

    Agrupa días con el mismo horario y detecta rangos contiguos.
    """
    activos = [w for w in windows if w.active]
    if not activos:
        return ""
    from collections import defaultdict

    grupos: dict[tuple[time, time], list[int]] = defaultdict(list)
    for w in activos:
        grupos[(w.start_time, w.end_time)].append(w.day_of_week)

    partes: list[str] = []
    for (st, en), dows in sorted(grupos.items(), key=lambda kv: min(_DOW_ORDEN[d] for d in kv[1])):
        ordenados = sorted(set(dows), key=lambda d: _DOW_ORDEN[d])
        # ¿rango contiguo en orden semanal?
        idxs = [_DOW_ORDEN[d] for d in ordenados]
        contiguo = idxs == list(range(idxs[0], idxs[0] + len(idxs)))
        if contiguo and len(ordenados) >= 2:
            dias_txt = f"{_DOW_NOMBRES[ordenados[0]]} a {_DOW_NOMBRES[ordenados[-1]]}"
        else:
            dias_txt = ", ".join(_DOW_NOMBRES[d] for d in ordenados)
        partes.append(f"{dias_txt} de {_fmt_hora_humana(st)} a {_fmt_hora_humana(en)}")
    return "; ".join(partes)


async def resumen_disponibilidad(settings: Settings | None = None) -> str:
    """Resumen humano del horario de Lily (consulta lily_availability)."""
    settings = settings or get_settings()
    if not settings.supabase_url:
        return ""
    windows = await _fetch_availability_windows(settings)
    return resumen_disponibilidad_humano(windows)


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


def _slots_del_dia(
    dia: datetime, duracion_min: int, windows: list[LilyAvailabilityWindow]
) -> list[datetime]:
    """Todos los slots posibles (inicio) del día `dia` según las ventanas de Lily."""
    dow = _dow_postgres_style(dia)
    slots: list[datetime] = []
    for w in windows:
        if w.day_of_week != dow or not w.active:
            continue
        current = dia.replace(
            hour=w.start_time.hour, minute=w.start_time.minute, second=0, microsecond=0
        )
        win_end = dia.replace(
            hour=w.end_time.hour, minute=w.end_time.minute, second=0, microsecond=0
        )
        while current + timedelta(minutes=duracion_min) <= win_end:
            slots.append(current)
            current = current + timedelta(minutes=w.slot_duration_minutes)
    return sorted(slots)


def _generar_candidatos_alternativos(
    fecha_hora_solicitada: datetime,
    duracion_min: int,
    windows: list[LilyAvailabilityWindow],
    now: datetime,
    max_dias_adelante: int = 7,
) -> list[datetime]:
    """Slots candidatos FUTUROS (> now) cercanos al horario solicitado, dentro de
    `max_dias_adelante` días. Incluye el MISMO día en horas válidas (ej. ofrecer
    'viernes 2pm' ante 'viernes 5pm'). NO verifica colisión con citas (lo hace el
    caller). Ordenados por cercanía a lo pedido."""
    candidatos: list[datetime] = []
    base = fecha_hora_solicitada
    for offset_dias in range(0, max_dias_adelante + 1):
        dia = base + timedelta(days=offset_dias)
        for current in _slots_del_dia(dia, duracion_min, windows):
            if current > now and current != fecha_hora_solicitada:
                candidatos.append(current)
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
    resumen = resumen_disponibilidad_humano(windows)

    # ¿Día laborable de Lily?
    if not _dia_es_laborable(fecha_hora, windows):
        alts = await _proponer_alternativas(
            fecha_hora, duracion_minutos, windows, settings, now_local
        )
        return AvailabilityResult(
            available=False,
            reason="dia_no_laborable",
            alternativas=alts,
            mensaje="Ese día Lily no está disponible.",
            resumen=resumen,
        )

    # ¿Dentro del horario?
    if not _slot_dentro_de_horario(fecha_hora, duracion_minutos, windows):
        alts = await _proponer_alternativas(
            fecha_hora, duracion_minutos, windows, settings, now_local
        )
        return AvailabilityResult(
            available=False,
            reason="fuera_de_horario",
            alternativas=alts,
            mensaje="Ese horario está fuera del rango de Lily.",
            resumen=resumen,
        )

    # ¿Choca con otra cita?
    rango_inicio = fecha_hora - timedelta(hours=2)
    rango_fin = fecha_hora + timedelta(hours=2)
    citas = await _fetch_appointments_in_range(settings, rango_inicio, rango_fin)
    if _slot_choca_con_citas(fecha_hora, duracion_minutos, citas):
        alts = await _proponer_alternativas(
            fecha_hora, duracion_minutos, windows, settings, now_local
        )
        return AvailabilityResult(
            available=False,
            reason="slot_ocupado",
            alternativas=alts,
            mensaje="Esa hora ya está ocupada.",
            resumen=resumen,
        )

    return AvailabilityResult(
        available=True, reason="ok", mensaje="Slot disponible.", resumen=resumen
    )


async def _proponer_alternativas(
    fecha_hora: datetime,
    duracion_min: int,
    windows: list[LilyAvailabilityWindow],
    settings: Settings,
    now: datetime,
    n: int = 3,
) -> list[datetime]:
    """Genera hasta `n` alternativas FUTURAS que pasan horario Y no chocan con citas."""
    candidatos = _generar_candidatos_alternativos(fecha_hora, duracion_min, windows, now)
    if not candidatos:
        return []

    rango_inicio = min(candidatos[0], fecha_hora) - timedelta(hours=1)
    rango_fin = max(candidatos[-1], fecha_hora) + timedelta(days=1)
    citas = await _fetch_appointments_in_range(settings, rango_inicio, rango_fin)

    seleccionadas: list[datetime] = []
    for cand in candidatos:
        if not _slot_choca_con_citas(cand, duracion_min, citas):
            seleccionadas.append(cand)
            if len(seleccionadas) >= n:
                break
    return seleccionadas


async def evaluar_dia(
    fecha_dia: datetime,
    *,
    duracion_min: int = 60,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> AvailabilityResult:
    """Evalúa un DÍA (sin hora específica) anclado al AHORA real. FIX 2026-06-02.

    Para el caso "el papá dio el día pero no la hora": decide si ese día es
    reservable considerando la hora actual y la disponibilidad real de Lily.

    Returns AvailabilityResult con:
    - available=True + `alternativas` = HORAS libres de ESE día (para ofrecerlas).
    - available=False + reason fecha_pasada (día pasado u hoy ya cerró) /
      dia_no_laborable / slot_ocupado, con `alternativas` = próximos slots reales.
    """
    settings = settings or get_settings()
    now_local = _to_monterrey(now or datetime.now(TZ_MONTERREY))
    dia = _to_monterrey(fecha_dia)

    if dia.date() < now_local.date():
        return AvailabilityResult(False, "fecha_pasada", [], "Ese día ya pasó.")
    if not settings.supabase_url:
        return AvailabilityResult(False, "supabase_error", [], "")
    windows = await _fetch_availability_windows(settings)
    if not windows:
        return AvailabilityResult(False, "supabase_error", [], "")
    resumen = resumen_disponibilidad_humano(windows)

    if not _dia_es_laborable(dia, windows):
        alts = await _proximos_slots(dia, duracion_min, windows, settings, now_local)
        return AvailabilityResult(
            False, "dia_no_laborable", alts, "Ese día Lily no atiende.", resumen
        )

    # Slots del día que TODAVÍA no pasaron (clave para "hoy 9pm").
    slots = [s for s in _slots_del_dia(dia, duracion_min, windows) if s > now_local]
    if not slots:
        alts = await _proximos_slots(
            dia + timedelta(days=1), duracion_min, windows, settings, now_local
        )
        return AvailabilityResult(
            False, "fecha_pasada", alts, "Hoy ya pasó el horario de atención.", resumen
        )

    citas = await _fetch_appointments_in_range(
        settings, slots[0] - timedelta(hours=1), slots[-1] + timedelta(hours=1)
    )
    libres = [s for s in slots if not _slot_choca_con_citas(s, duracion_min, citas)]
    if not libres:
        alts = await _proximos_slots(
            dia + timedelta(days=1), duracion_min, windows, settings, now_local
        )
        return AvailabilityResult(False, "slot_ocupado", alts, "Ese día ya está lleno.", resumen)

    return AvailabilityResult(True, "ok", libres, "", resumen)


async def _proximos_slots(
    desde: datetime,
    duracion_min: int,
    windows: list[LilyAvailabilityWindow],
    settings: Settings,
    now: datetime,
    n: int = 3,
) -> list[datetime]:
    """Próximos `n` slots libres (futuros) desde `desde`, en los siguientes 7 días."""
    base = max(_to_monterrey(desde), now)
    cand: list[datetime] = []
    for off in range(0, 8):
        for s in _slots_del_dia(base + timedelta(days=off), duracion_min, windows):
            if s > now:
                cand.append(s)
    cand = sorted(set(cand))
    if not cand:
        return []
    citas = await _fetch_appointments_in_range(
        settings, cand[0] - timedelta(hours=1), cand[-1] + timedelta(hours=1)
    )
    libres = [s for s in cand if not _slot_choca_con_citas(s, duracion_min, citas)]
    return libres[:n]


async def proximos_dias_habiles(
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    cantidad: int = 3,
    duracion_min: int = 60,
) -> list[datetime]:
    """Próximos `cantidad` DÍAS hábiles con al menos un slot LIBRE (futuro). Salta
    hoy si ya pasó el último slot, los fines de semana y los días sin ventana o llenos.
    Devuelve datetimes a medianoche (solo importa la fecha). [] si no hay/falla."""
    settings = settings or get_settings()
    base_now = _to_monterrey(now or datetime.now(TZ_MONTERREY))
    windows = await _fetch_availability_windows(settings)
    if not windows:
        return []
    # Reúne slots libres de las próximas ~3 semanas y agrupa por día.
    cand: list[datetime] = []
    for off in range(0, 22):
        dia = (base_now + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
        for s in _slots_del_dia(dia, duracion_min, windows):
            if s > base_now:
                cand.append(s)
    if not cand:
        return []
    cand.sort()
    citas = await _fetch_appointments_in_range(
        settings, cand[0] - timedelta(hours=1), cand[-1] + timedelta(hours=1)
    )
    dias: list[datetime] = []
    vistos: set = set()
    for s in cand:
        d = s.date()
        if d in vistos:
            continue
        if _slot_choca_con_citas(s, duracion_min, citas):
            # ese slot está ocupado; revisa si el día tiene OTRO libre
            libres_dia = [
                x
                for x in _slots_del_dia(s.replace(hour=0, minute=0), duracion_min, windows)
                if x > base_now and not _slot_choca_con_citas(x, duracion_min, citas)
            ]
            if not libres_dia:
                continue
        vistos.add(d)
        dias.append(s.replace(hour=0, minute=0, second=0, microsecond=0))
        if len(dias) >= cantidad:
            break
    return dias
