"""Extractor de fecha/hora para citas (Bloque C.1 PASO 3).

Usa gpt-4o-mini con structured output JSON para convertir expresiones
en español mexicano ("el martes 10am", "mañana a las 3") a fecha/hora
exactas en zona America/Monterrey.

Retorna None si la fecha es ambigua o no extraíble — el orchestrator
entonces deja que Sofía pida aclaración.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.adapters.openai_client import get_openai

log = logging.getLogger(__name__)

TZ_MONTERREY = ZoneInfo("America/Monterrey")
CONFIDENCE_MIN = 0.7


# ============================================================
# Detección de expresión temporal (FIX 1+3 — 2026-05-29)
# ============================================================
#
# El flujo de agendado (fecha + gate de 6 datos + Maps) estaba acoplado a que
# el intent fuese QUIERE_AGENDAR. En conversación fragmentada el papá responde
# en fragmentos ("Viernes", "Mañana", "Mejor lunes") que el classifier NO marca
# como QUIERE_AGENDAR, así que TODO el andamiaje determinístico se omitía y el
# LLM improvisaba la fecha (mal). Este detector permite al orchestrator disparar
# el resolver de fecha en CUALQUIER turno con expresión temporal.
_TEMPORAL_RE = re.compile(
    r"\b("
    r"hoy|ma[ñn]ana|pasado\s+ma[ñn]ana|"
    r"lunes|martes|mi[ée]rcoles|miercoles|jueves|viernes|s[áa]bado|sabado|domingo|"
    r"pr[óo]xim[ao]\s+semana|esta\s+semana|entre\s+semana|fin\s+de\s+semana|finde|"
    r"a\s+las\s+\d{1,2}|"
    r"\d{1,2}\s*(?:am|pm|a\.?\s?m|p\.?\s?m|hrs?|horas?)|"
    r"\d{1,2}\s*[:.]\s*\d{2}"
    r")\b",
    re.IGNORECASE,
)


def contiene_expresion_temporal(mensaje: str) -> bool:
    """True si el mensaje menciona un día/hora/expresión temporal accionable.

    Usado por el orchestrator para decidir si invoca el resolver de fecha y el
    flujo de agendado, independientemente del intent clasificado.
    """
    return bool(_TEMPORAL_RE.search(mensaje or ""))


_DIAS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def fecha_humana_solo_dia(fecha_iso: str) -> str | None:
    """'2026-06-01' → 'lunes 1 de junio'. None si el formato es inválido."""
    try:
        d = date.fromisoformat(fecha_iso)
    except (ValueError, TypeError):
        return None
    return f"{_DIAS_ES[d.weekday()]} {d.day} de {_MESES_ES[d.month - 1]}"


@dataclass
class AppointmentDateTime:
    """Fecha/hora extraída de un mensaje. Si fecha o hora son None,
    la extracción está incompleta y el orchestrator pide aclaración."""

    fecha: str | None  # YYYY-MM-DD
    hora: str | None  # HH:MM (24h)
    confidence: float
    razonamiento: str

    @property
    def es_completo(self) -> bool:
        return self.fecha is not None and self.hora is not None

    @property
    def es_alta_confianza(self) -> bool:
        return self.confidence >= CONFIDENCE_MIN

    def to_datetime(self) -> datetime | None:
        """Combina fecha + hora en datetime aware (America/Monterrey).

        Retorna None si faltan campos o el formato es inválido.
        """
        if not self.es_completo:
            return None
        try:
            dt = datetime.strptime(f"{self.fecha} {self.hora}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return dt.replace(tzinfo=TZ_MONTERREY)


_SYSTEM_PROMPT_TPL = """Eres un extractor de fechas en español mexicano. La zona horaria es America/Monterrey. Hoy es {fecha_actual} ({dia_semana}). Convierte expresiones a fecha y hora exactas.

REGLAS:
1. Si el papá no especifica AM/PM en una hora ambigua (ej. "a las 3"), asume horario laboral típico: 9-17h → 3 PM (15:00), no 3 AM.
2. "Mañana" = fecha_actual + 1 día.
3. "El martes" = el próximo martes (si hoy es martes y aún no llegó la hora, asume PRÓXIMO martes, no hoy).
4. "La próxima semana" sin día = ambiguo → retorna fecha=null, hora=null.
5. "Cualquier día" / "el que sea" = ambiguo → null.
6. Solo retorna fechas FUTURAS. Nunca pasadas.
7. NUNCA inventes una fecha. Si dudas, retorna null y deja confidence bajo (<0.7).

Devuelve EXCLUSIVAMENTE JSON con esta estructura:
{{
  "fecha": "YYYY-MM-DD" o null,
  "hora": "HH:MM" (24h) o null,
  "confidence": 0.0-1.0,
  "razonamiento": "una oración corta"
}}"""


_DIAS_SEMANA_ES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]


def _build_system_prompt(now: datetime) -> str:
    fecha_actual = now.strftime("%Y-%m-%d")
    dia_semana = _DIAS_SEMANA_ES[now.weekday()]
    return _SYSTEM_PROMPT_TPL.format(fecha_actual=fecha_actual, dia_semana=dia_semana)


def _parse_result(raw: str, fallback_razonamiento: str = "") -> AppointmentDateTime:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("appointment_extractor non-json", extra={"raw": raw[:200], "err": str(exc)})
        return AppointmentDateTime(
            fecha=None,
            hora=None,
            confidence=0.0,
            razonamiento=fallback_razonamiento or "parse_error",
        )

    fecha = data.get("fecha")
    hora = data.get("hora")
    if isinstance(fecha, str) and not fecha.strip():
        fecha = None
    if isinstance(hora, str) and not hora.strip():
        hora = None
    confidence_raw = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    razonamiento = str(data.get("razonamiento") or "")[:300]

    return AppointmentDateTime(
        fecha=fecha if isinstance(fecha, str) else None,
        hora=hora if isinstance(hora, str) else None,
        confidence=confidence,
        razonamiento=razonamiento,
    )


async def extract_datetime(
    mensaje: str,
    *,
    now: datetime | None = None,
) -> AppointmentDateTime:
    """Extrae fecha/hora de un mensaje. Siempre devuelve un AppointmentDateTime;
    el caller decide si es accionable vía `es_completo` y `es_alta_confianza`.

    Args:
        mensaje: texto del papá ("el martes 10am", "mañana a las 3", etc.)
        now: opcional, datetime actual para tests determinísticos. Default = ahora.

    Returns:
        AppointmentDateTime con fecha/hora/confidence/razonamiento.
    """
    openai = get_openai()
    if not openai.is_configured():
        log.warning("openai not configured, returning empty appointment datetime")
        return AppointmentDateTime(
            fecha=None, hora=None, confidence=0.0, razonamiento="openai not configured"
        )

    now_local = now or datetime.now(TZ_MONTERREY)
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=TZ_MONTERREY)

    system_prompt = _build_system_prompt(now_local)
    try:
        raw = await openai.classify(text=mensaje, instructions=system_prompt)
    except Exception as exc:
        log.warning("appointment_extractor api error", extra={"error": str(exc)})
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="api_error")

    return _parse_result(raw)
