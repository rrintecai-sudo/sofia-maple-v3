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
from datetime import date, datetime, timedelta
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
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def fecha_humana_solo_dia(fecha_iso: str, now: datetime | None = None) -> str | None:
    """'2026-06-01' → 'lunes 1 de junio'. None si el formato es inválido.

    Si `now` se pasa, etiqueta 'hoy,'/'mañana,' cuando el día cae hoy/mañana en hora
    de Saltillo (America/Monterrey) — mismo criterio que la propuesta de días."""
    try:
        d = date.fromisoformat(fecha_iso)
    except (ValueError, TypeError):
        return None
    etiqueta = ""
    if now is not None:
        base = now if now.tzinfo else now.replace(tzinfo=TZ_MONTERREY)
        hoy = base.astimezone(TZ_MONTERREY).date()
        if d == hoy:
            etiqueta = "hoy, "
        elif (d - hoy).days == 1:
            etiqueta = "mañana, "
    return f"{etiqueta}{_DIAS_ES[d.weekday()]} {d.day} de {_MESES_ES[d.month - 1]}"


# ============================================================
# Extractor determinístico de HORA suelta (FIX 2026-06-01)
# ============================================================
#
# El extractor LLM solo resuelve la hora de forma fiable cuando viene junto a la
# fecha. En conversación fragmentada el papá manda la hora sola ("2pm", "a las
# 2") en un mensaje aparte, y el LLM la devolvía con baja confianza o null → el
# slot de hora quedaba vacío y la cita nunca cerraba. Este fallback resuelve la
# hora por código. Solo dispara si hay un INDICADOR de hora (am/pm, ":MM",
# "de la tarde", "a las") para no confundir "4 años" o "kinder 2" con una hora.

_HORA_AMPM_RE = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)\b", re.IGNORECASE
)
# FIX (a) 2026-06-01: meridiano BARE pegado al número, sin 'm' ("10a", "2p") —
# typo común del papá ("viernes 10a,"). Solo pegado (sin espacio) para no
# confundir "4 a" / "4 años".
_HORA_BARE_RE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?(a|p)\b", re.IGNORECASE)
# FIX (a): formato 24h con sufijo hrs/horas/h ("10hrs", "14h", "10 horas").
_HORA_HRS_RE = re.compile(
    r"\b([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*(?:hrs?|horas?|h)\b", re.IGNORECASE
)
_HORA_FRANJA_RE = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(?:de|en|por)\s+la\s+(ma[ñn]ana|tarde|noche)\b",
    re.IGNORECASE,
)
_HORA_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_HORA_ALAS_RE = re.compile(r"\ba\s+las?\s+(\d{1,2})(?:[:.](\d{2}))?\b", re.IGNORECASE)


def extraer_hora_simple(mensaje: str) -> str | None:
    """Devuelve 'HH:MM' (24h) si el mensaje contiene una hora con indicador, o None.

    Ejemplos: '2pm'→'14:00', '2:30 p.m.'→'14:30', '10am'→'10:00', '10a'→'10:00',
    '10hrs'→'10:00', '14:00'→'14:00', 'a las 2'→'14:00', '9 de la mañana'→'09:00'.
    'tengo 4 años' / 'kinder 2' → None (sin indicador de hora).
    """
    m = (mensaje or "").lower()

    am_pm = _HORA_AMPM_RE.search(m)
    if am_pm:
        h = int(am_pm.group(1))
        mi = int(am_pm.group(2) or 0)
        mer = am_pm.group(3).replace(".", "").replace(" ", "")
        if mer.startswith("p") and h != 12:
            h += 12
        elif mer.startswith("a") and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    bare = _HORA_BARE_RE.search(m)
    if bare:
        h = int(bare.group(1))
        mi = int(bare.group(2) or 0)
        if bare.group(3).lower() == "p" and h != 12:
            h += 12
        elif bare.group(3).lower() == "a" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    hrs = _HORA_HRS_RE.search(m)
    if hrs:
        return f"{int(hrs.group(1)):02d}:{int(hrs.group(2) or 0):02d}"

    franja = _HORA_FRANJA_RE.search(m)
    if franja:
        h = int(franja.group(1))
        mi = int(franja.group(2) or 0)
        f = franja.group(3)
        if f in ("tarde", "noche") and h != 12:
            h += 12
        elif f.startswith("ma") and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    h24 = _HORA_24_RE.search(m)
    if h24:
        return f"{int(h24.group(1)):02d}:{int(h24.group(2)):02d}"

    alas = _HORA_ALAS_RE.search(m)
    if alas:
        h = int(alas.group(1))
        mi = int(alas.group(2) or 0)
        # Horario laboral de Lily (8:00 a 15:00): una hora de 1 a 7 sin meridiano es PM.
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    return None


# Número SUELTO como hora — SOLO cuando el código acaba de pedir la hora (contexto).
# "10" → 10:00; "1" → 13:00 (1-7 = PM por el horario de Lily); "13" → 13:00.
_NUM_SUELTO_HORA_RE = re.compile(
    r"^\s*(?:a\s+las\s+)?(\d{1,2})(?:[:.]([0-5]\d))?\s*(?:hrs?|horas?|h)?\s*\.?\s*$",
    re.IGNORECASE,
)


def extraer_hora_de_numero_suelto(mensaje: str) -> str | None:
    """'10' → '10:00'; '1' → '13:00'; '13' → '13:00'; '10:30' → '10:30'. None si el
    mensaje no es básicamente un número. Usar SOLO cuando el gate pidió la hora."""
    m = _NUM_SUELTO_HORA_RE.match(mensaje or "")
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if 1 <= h <= 7:  # franja PM por el horario de Lily (8-15)
        h += 12
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return f"{h:02d}:{mi:02d}"
    return None


# ============================================================
# Confirmación general + fecha explícita (FIX (b) 2026-06-01)
# ============================================================
#
# Robustez por confirmación: cuando Sofía PROPONE un valor (fecha, hora, grado)
# y el papá confirma ("sí", "dale", "correcto", "ok"), el código captura el valor
# propuesto al slot AUNQUE el extractor haya fallado el mensaje del papá. Así un
# typo ("10a") o una fecha que solo Sofía escribió quedan rescatados.

_CONFIRMA_RE = re.compile(
    r"^\s*(?:"
    r"s[ií]|sip|sep|dale|ok(?:ay|ey)?|va|sale|"
    r"correcto|exacto(?:mente)?|claro|as[ií]\s+es|"
    r"de\s+acuerdo|perfecto|confirmo|afirmativo|aj[aá]|"
    r"est[aá]\s+bien|bien|t[aá]\s+bien|👍|✅"
    r")"
    r"(?:[\s,]+(?:dale|porfa|por\s+favor|claro|correcto|exacto|gracias|s[ií]|"
    r"as[ií]\s+es|est[aá]\s+bien|bien|va|ok))*"
    r"[\s\.\!]*$",
    re.IGNORECASE,
)


def es_confirmacion(mensaje: str) -> bool:
    """True si el mensaje es una confirmación pura ('sí', 'dale', 'ok', 'sí dale').

    NO matchea si agrega información nueva ('sí pero el lunes') — solo afirmación.
    """
    return bool(_CONFIRMA_RE.match((mensaje or "").strip()))


_MESES_NUM: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_FECHA_DIA_MES_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)\b",
    re.IGNORECASE,
)


def extraer_fecha_explicita(texto: str, now: datetime | None = None) -> str | None:
    """'viernes 5 de junio' / '5 de junio' → '2026-06-05' (próxima ocurrencia futura).

    Determinístico: usado para rescatar la fecha que Sofía PROPONE cuando el papá
    confirma. Devuelve None si no hay un 'D de MES' explícito.
    """
    m = _FECHA_DIA_MES_RE.search((texto or "").lower())
    if not m:
        return None
    dia = int(m.group(1))
    mes = _MESES_NUM[m.group(2)]
    base = now or datetime.now(TZ_MONTERREY)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ_MONTERREY)
    try:
        d = date(base.year, mes, dia)
    except ValueError:
        return None
    # Si ya pasó este año, asumimos el próximo año.
    if d < base.astimezone(TZ_MONTERREY).date():
        try:
            d = date(base.year + 1, mes, dia)
        except ValueError:
            return None
    return d.isoformat()


# ============================================================
# Día de la semana suelto → próxima ocurrencia (FIX 2026-06-02)
# ============================================================
#
# "el viernes" / "lunes" / "este jueves" debe resolverse DETERMINÍSTICAMENTE a la
# próxima ocurrencia futura de ese día, SIN preguntar "¿el 5 o el 12?". Antes esto
# dependía del LLM extract_datetime, que para días cercanos devolvía baja confianza
# o no fijaba el slot → el flujo entraba en "pedir día" y Haiku improvisaba la
# desambiguación. El clasificador/extractor LLM no debe ser load-bearing.

_DIA_SEMANA_NUM: dict[str, int] = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}
_DIA_SEMANA_RE = re.compile(
    r"\b(?:el|este|esta|pr[óo]xim[oa])?\s*"
    r"(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\b",
    re.IGNORECASE,
)


def extraer_proximo_dia_semana(texto: str, now: datetime | None = None) -> str | None:
    """'el viernes' / 'lunes' / 'este jueves' → 'YYYY-MM-DD' de la PRÓXIMA ocurrencia.

    Determinístico: resuelve un día de semana suelto a su próxima ocurrencia futura
    en America/Monterrey. Si HOY es ese día y aún no pasó el horario de atención
    (antes de las 15:00) usa hoy; si ya cerró, la próxima semana. Devuelve None si
    el texto no menciona un día de semana.
    """
    m = _DIA_SEMANA_RE.search((texto or "").lower())
    if not m:
        return None
    target = _DIA_SEMANA_NUM[m.group(1)]
    base = now or datetime.now(TZ_MONTERREY)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ_MONTERREY)
    base_local = base.astimezone(TZ_MONTERREY)
    delta = (target - base_local.weekday()) % 7
    if delta == 0 and base_local.hour >= 15:
        delta = 7  # hoy es ese día pero ya pasó el horario → próxima semana
    return (base_local.date() + timedelta(days=delta)).isoformat()


# Cierre del horario de atención de Lily (8 a.m. a 3 p.m.). Tras esta hora, "hoy"
# ya no es agendable → se ofrece el próximo día hábil.
_HORA_CIERRE_LILY = 15
_HOY_RE = re.compile(r"\bhoy\b", re.IGNORECASE)
_PASADO_MANANA_RE = re.compile(r"\bpasado\s+ma[ñn]ana\b", re.IGNORECASE)
# "mañana" = día siguiente, PERO no cuando es "la mañana"/"en la mañana"/"por la
# mañana"/"de la mañana" → eso es FRANJA HORARIA, no el día. Sin esta guarda,
# "en la mañana" (respuesta a la hora) pisaba el día ya elegido (bug real: perdía
# "jueves 25" y se iba a "mañana, martes 23"). La franja la resuelve el paso de hora.
_MANANA_RE = re.compile(r"(?<!la\s)\bma[ñn]ana\b", re.IGNORECASE)


def _proximo_dia_habil(d: date) -> date:
    """Avanza `d` hasta el próximo día lun-vie (si cae sábado/domingo)."""
    while d.weekday() >= 5:  # 5=sábado, 6=domingo
        d = d + timedelta(days=1)
    return d


def extraer_fecha_relativa(texto: str, now: datetime | None = None) -> str | None:
    """'hoy' / 'mañana' / 'pasado mañana' → 'YYYY-MM-DD' determinístico.

    - 'hoy': la fecha de hoy; si ya pasó el horario de atención (>=15:00) o cae en
      fin de semana, ofrece el PRÓXIMO día hábil.
    - 'mañana' / 'pasado mañana': día siguiente / +2 (literal; la validación de
      disponibilidad descarta fin de semana y propone alternativas).
    Devuelve None si el texto no menciona una fecha relativa.
    """
    t = (texto or "").lower()
    base = now or datetime.now(TZ_MONTERREY)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ_MONTERREY)
    base_local = base.astimezone(TZ_MONTERREY)
    if _PASADO_MANANA_RE.search(t):
        return (base_local.date() + timedelta(days=2)).isoformat()
    if _MANANA_RE.search(t):
        return (base_local.date() + timedelta(days=1)).isoformat()
    if _HOY_RE.search(t):
        d = base_local.date()
        if base_local.hour >= _HORA_CIERRE_LILY:
            d = d + timedelta(days=1)  # ya cerró hoy → próximo día
        return _proximo_dia_habil(d).isoformat()
    return None


def motivo_ajuste_fecha_relativa(texto: str, now: datetime | None = None) -> str | None:
    """Si el papá dijo 'hoy' pero la fecha se MOVIÓ a otro día (cerró el horario o es
    fin de semana), devuelve la RAZÓN legible para que Sofía la explique en vez de
    saltar de día en silencio. None si no se movió. (El caso 'mañana'→fin de semana
    lo explica el path de disponibilidad.)"""
    t = (texto or "").lower()
    base = now or datetime.now(TZ_MONTERREY)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ_MONTERREY)
    base_local = base.astimezone(TZ_MONTERREY)
    if _HOY_RE.search(t):
        if base_local.hour >= _HORA_CIERRE_LILY:
            return "Hoy ya cerramos el horario de visitas"
        if base_local.date().weekday() >= 5:
            return "Hoy no atendemos porque es fin de semana"
    return None


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


_SYSTEM_PROMPT_TPL = """Eres un extractor de fechas en español mexicano. La zona horaria es America/Monterrey. AHORA mismo es {fecha_actual} ({dia_semana}) a las {hora_actual}. Convierte expresiones a fecha y hora exactas, SIEMPRE en el futuro respecto a este momento.

REGLAS:
1. Si el papá no especifica AM/PM en una hora ambigua (ej. "a las 2"), asume el horario de atención de Lily: lunes a viernes de 8 a.m. a 3 p.m. (8-15h). "a las 2" → 2 PM (14:00), no 2 AM.
2. "Mañana" = fecha de hoy + 1 día.
3. "El <día de la semana>" = la PRÓXIMA ocurrencia futura de ese día. Si HOY es ese día pero ya pasó el horario de atención (después de las 15:00) o ya es de noche, NO uses hoy: usa el de la PRÓXIMA semana.
4. NUNCA devuelvas una fecha/hora que ya pasó respecto al momento actual ({fecha_actual} {hora_actual}). Si el papá pide "hoy" pero ya es tarde, devuelve la fecha de hoy igual y deja que el sistema valide (pero nunca un día/hora anterior a ahora).
5. "La próxima semana" sin día = ambiguo → fecha=null, hora=null. "Cualquier día" / "el que sea" = null.
6. NUNCA inventes una fecha. Si dudas, retorna null y deja confidence bajo (<0.7).

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


_ORDINAL_OPCION: list[tuple] = [
    (re.compile(r"\b(?:el\s+)?(?:primer[oa]?|1[°º]|la\s+primera)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(?:el\s+)?(?:segund[oa]|2[°º]|la\s+segunda)\b", re.IGNORECASE), 1),
    (
        re.compile(r"\b(?:el\s+)?(?:tercer[oa]?|3[°º]|la\s+tercera|[úu]ltim[oa])\b", re.IGNORECASE),
        2,
    ),
]

# Ordinal/número seguido de "de {nivel}" = grado, NO una opción de fecha.
_GRADO_EN_FRASE_RE = re.compile(
    r"\b(?:primer[oa]?|segund[oa]|tercer[oa]?|cuart[oa]|quint[oa]|sext[oa]|[1-9][°º]?)\s+"
    r"de\s+(?:kinder|k[íi]nder|preescolar|primaria|secundaria)\b",
    re.IGNORECASE,
)


def elegir_opcion_dia(mensaje: str, opciones_iso: list[str] | None) -> str | None:
    """Matchea la respuesta del papá contra las fechas OFRECIDAS (ISO). Reconoce:
    ordinal ('el primero'/'la segunda'), nombre del día ('el jueves') y número de día
    del mes ('11'). Devuelve la fecha ISO elegida o None si no matchea ninguna."""
    if not opciones_iso:
        return None
    t = (mensaje or "").lower()
    try:
        fechas = [date.fromisoformat(o) for o in opciones_iso]
    except (ValueError, TypeError):
        return None
    # Defensa: un ordinal en FRASE DE GRADO ("primero DE secundaria") NO es elegir la
    # "primera opción" — es una pregunta de contenido. Ignora ordinales en ese caso.
    frase_grado = bool(_GRADO_EN_FRASE_RE.search(t))
    # 1) ordinal ("el primero", "la segunda", "el último")
    for rx, idx in _ORDINAL_OPCION:
        if not frase_grado and rx.search(t):
            if idx == 2 and "últim" in t:
                return fechas[-1].isoformat()
            if idx < len(fechas):
                return fechas[idx].isoformat()
    # 2) nombre del día de la semana ("el jueves")
    for dow, nombre in enumerate(_DIAS_SEMANA_ES):
        nombre_norm = nombre.replace("é", "e").replace("á", "a")
        if re.search(rf"\b{nombre}\b", t) or re.search(rf"\b{nombre_norm}\b", t):
            for f in fechas:
                if f.weekday() == dow:
                    return f.isoformat()
    # 3) número de día del mes ("11", "el 12") — pero NO si es frase de grado.
    if not frase_grado:
        m = re.search(r"\b(\d{1,2})\b", t)
        if m:
            dom = int(m.group(1))
            for f in fechas:
                if f.day == dom:
                    return f.isoformat()
    return None


def mensaje_resuelve_fecha(
    mensaje: str, opciones_iso: list[str] | None, now: datetime | None = None
) -> bool:
    """True si el mensaje parsea como una FECHA válida (opción/explícita/relativa/
    'esta semana'). Mismas vías que el handler — para decidir la precedencia: una
    fecha válida le gana a la pausa de info."""
    if extraer_fecha_explicita(mensaje, now):
        return True
    if elegir_opcion_dia(mensaje, opciones_iso):
        return True
    if extraer_fecha_relativa(mensaje, now):
        return True
    if extraer_proximo_dia_semana(mensaje, now):
        return True
    return bool(re.search(r"\bsemana\b", (mensaje or "").lower()))


def mensaje_resuelve_hora(mensaje: str, campo_pedido_prev: str | None) -> bool:
    """True si el mensaje parsea como una HORA válida (en el paso de la hora)."""
    if extraer_hora_simple(mensaje):
        return True
    if campo_pedido_prev == "hora" and extraer_hora_de_numero_suelto(mensaje):
        return True
    return False


async def elegir_dia_de_opciones_llm(
    mensaje: str, opciones_iso: list[str], now: datetime | None = None
) -> str | None:
    """CLAUDE-CONDUCE: cuando el resolver rígido no eligió un día pero YA ofrecimos
    fechas concretas, el LLM mapea una respuesta vaga ('esta semana', 'la próxima',
    'cualquiera', 'la más pronto', 'tú dime') a UNA de las opciones ofrecidas.

    El código sigue siendo dueño de QUÉ fechas existen (vienen en `opciones_iso`,
    calculadas desde lily_availability); el LLM solo hace el JUICIO de cuál encaja.
    Devuelve un ISO 'YYYY-MM-DD' que SIEMPRE pertenece a `opciones_iso`, o None.
    """
    if not opciones_iso:
        return None
    openai = get_openai()
    if not openai.is_configured():
        return None
    if now is None:
        now = datetime.now(TZ_MONTERREY)
    lineas: list[str] = []
    for iso in opciones_iso:
        try:
            d = datetime.fromisoformat(iso)
        except ValueError:
            continue
        lineas.append(f"- {iso} ({_DIAS_SEMANA_ES[d.weekday()]} {d.day})")
    if not lineas:
        return None
    instructions = (
        f"Hoy es {now.strftime('%Y-%m-%d')} ({_DIAS_SEMANA_ES[now.weekday()]}). "
        "Un papá respondió a '¿qué día te queda mejor para la visita?'. Le ofrecimos "
        "EXACTAMENTE estas fechas disponibles:\n" + "\n".join(lineas) + "\n\n"
        'Devuelve SOLO un JSON {"fecha":"YYYY-MM-DD"} eligiendo la fecha de la lista '
        "que mejor corresponde a su respuesta:\n"
        "- 'esta semana' -> la primera fecha de la lista en la semana actual.\n"
        "- 'la proxima'/'la que viene' -> la primera de la semana siguiente.\n"
        "- 'cualquiera'/'la mas pronto'/'tu dime'/'el que sea' -> la primera de la lista.\n"
        '- Si su respuesta NO elige dia (pregunta de info, otro tema) -> {"fecha":null}.\n'
        "La fecha DEBE ser una de la lista, copiada igual. Responde SOLO el JSON."
    )
    try:
        raw = await openai.classify(text=mensaje, instructions=instructions)
    except Exception as exc:  # nunca rompe el turno
        log.warning("elegir_dia_de_opciones_llm error", extra={"err": str(exc)})
        return None
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    fecha = data.get("fecha")
    if isinstance(fecha, str) and fecha.strip() in opciones_iso:
        return fecha.strip()
    return None


def _build_system_prompt(now: datetime) -> str:
    fecha_actual = now.strftime("%Y-%m-%d")
    dia_semana = _DIAS_SEMANA_ES[now.weekday()]
    hora_actual = now.strftime("%H:%M")
    return _SYSTEM_PROMPT_TPL.format(
        fecha_actual=fecha_actual, dia_semana=dia_semana, hora_actual=hora_actual
    )


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
