"""Compositor del system prompt según el estado de la conversación.

Carga archivos modulares de `app/core/prompts/` y los compone en una lista de
bloques compatible con la API de Anthropic Messages. Marca como cacheables los
bloques estables (identity, rules, vocabulario, journey de la fase activa).

Decisión: ver ARCHITECTURE §6 y DECISIONS ADR (prompts modulares + caching).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.core.state import EstadoConversacion, FaseJourney, Modo

log = logging.getLogger(__name__)

TZ_MONTERREY = ZoneInfo("America/Monterrey")

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


def _hoy_humano(now: datetime | None = None) -> str:
    """Devuelve 'jueves 28 de mayo de 2026' en zona America/Monterrey.

    Parámetro `now` se inyecta en tests para determinismo.
    """
    dt = now or datetime.now(TZ_MONTERREY)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MONTERREY)
    else:
        dt = dt.astimezone(TZ_MONTERREY)
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]} de {dt.year}"


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Base de conocimiento OFICIAL (filtrada por Gaby/Lili) — fuente de verdad de
# COMPORTAMIENTO y CONOCIMIENTO. Vive fuera de prompts/ (en app/kb/).
KB_OFICIAL_PATH = Path(__file__).resolve().parent.parent / "kb" / "sofia_kb_oficial.md"

# Archivos cacheables que se cargan SIEMPRE
_ALWAYS_FILES = ("identity.md", "rules.md", "vocabulario.md")

_KB_OFICIAL_HEADER = (
    "# BASE DE CONOCIMIENTO OFICIAL DE MAPLE COLLÈGE — FUENTE DE VERDAD\n\n"
    "Este documento fue filtrado y aprobado por el colegio (Gaby y Lili). Es tu fuente "
    "de verdad de CONOCIMIENTO y COMPORTAMIENTO: tono, escena observable, detalle por "
    "nivel y grado, manejo de objeciones, prohibiciones. **Síguelo al pie de la letra y "
    "apégate a su contenido — no lo parafrasees a tu manera ni inventes información que "
    "no esté aquí.** Para DATOS duros (costos, horarios, estancias) usa SIEMPRE las "
    "cifras que el sistema te inyecta en el turno, nunca las de este documento.\n\n"
    "**REGLA ANTI-INVENTO (crítica):** para cualquier servicio o detalle que NO esté "
    "escrito en este documento ni en los datos del sistema (transporte/rutas, comedor/"
    "menús, uniformes, alberca, horario flexible, actividades específicas, etc.) NO "
    "sabes la respuesta: por lo tanto **NO digas 'sí' NI 'no'**, no lo afirmes ni lo "
    "niegues ni lo describas. Di EXACTAMENTE algo como: 'Es buena pregunta — ese dato "
    "lo confirma el equipo. En la visita lo resuelves con certeza.' Inventar un 'sí' "
    "(ej. 'sí tenemos transporte') es una falta grave. Ante la duda, defiere.\n\n"
    "---\n\n"
)


@lru_cache(maxsize=1)
def load_kb_oficial() -> str:
    """Carga la base de conocimiento oficial para el system prompt.

    LATENCIA: quita el DETALLE POR GRADO/MODALIDAD (~2.4k tokens) — ese contenido ya se
    INYECTA por turno desde el funnel (texto exacto del grado/modalidad), así que en el
    prompt es duplicado puro y solo añade latencia. El archivo completo se conserva para
    que el funnel lo lea.
    """
    if not KB_OFICIAL_PATH.exists():
        log.warning("KB oficial no encontrada en %s", KB_OFICIAL_PATH)
        return ""
    text = KB_OFICIAL_PATH.read_text(encoding="utf-8")
    i = text.find("## DETALLE POR GRADO")
    j = text.find("### Bullying")
    if i != -1 and j != -1 and j > i:
        text = (
            text[:i]
            + "## DETALLE POR GRADO\n\n*(El contenido específico de cada grado y de cada "
            "modalidad de maternal se INYECTA en cada turno desde el documento — no se "
            "repite aquí. Cuando describas un grado, usa el contenido que el sistema te "
            "inyecta en ese turno.)*\n\n"
            + text[j:]
        )
    return text


# Mapeo fase → archivo journey
_JOURNEY_FILES: dict[FaseJourney, str] = {
    FaseJourney.BIENVENIDA: "journey/bienvenida.md",
    FaseJourney.DESCUBRIMIENTO: "journey/descubrimiento.md",
    FaseJourney.EDUCACION: "journey/educacion.md",
    FaseJourney.INFORMACION: "journey/informacion.md",
    FaseJourney.OBJECIONES: "journey/objeciones.md",
    FaseJourney.AGENDADO: "journey/agendado.md",
    FaseJourney.POST_AGENDADO: "journey/post_agendado.md",
}


@lru_cache(maxsize=32)
def load_prompt_file(relative_path: str) -> str:
    """Carga un archivo de prompt. Cacheado para performance.

    Strip del frontmatter YAML si existe (sólo deja el cuerpo).
    """
    full = PROMPTS_DIR / relative_path
    if not full.exists():
        raise FileNotFoundError(f"Prompt file not found: {full} (cwd={Path.cwd()})")
    text = full.read_text(encoding="utf-8")
    # Strip YAML frontmatter delimitado por ---
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip("\n")
    return text


def clear_cache() -> None:
    """Para testing: borra el cache de archivos."""
    load_prompt_file.cache_clear()


def _datos_capturados_block(estado: EstadoConversacion) -> str | None:
    """Construye un bloque dinámico con los datos ya capturados del papá.

    Este bloque es el principal mecanismo anti-pregunta-repetida: si el papá ya
    dio el nivel, edad, escuela actual, etc., se inyecta aquí para que el modelo
    "lo sepa" explícitamente.
    """
    capt = estado.estado_capturado
    lines: list[str] = []

    if capt.nombre_papa:
        lines.append(f"- Nombre del papá/mamá: {capt.nombre_papa}")
    if capt.telefono:
        lines.append(f"- Teléfono: {capt.telefono}")
    if capt.hijos:
        for i, hijo in enumerate(capt.hijos, 1):
            partes = []
            if hijo.nombre:
                partes.append(f"nombre={hijo.nombre}")
            if hijo.edad is not None:
                partes.append(f"edad={hijo.edad}")
            if hijo.nivel:
                partes.append(f"nivel={hijo.nivel.value}")
            if hijo.grado:
                partes.append(f"grado={hijo.grado}")
            if hijo.escuela_actual:
                partes.append(f"escuela_actual={hijo.escuela_actual}")
            if hijo.diagnostico:
                partes.append(f"diagnostico={hijo.diagnostico}")
            lines.append(f"- Hijo {i}: {', '.join(partes)}")

    if capt.nivel_buscado_actual:
        lines.append(f"- Nivel del que se está hablando AHORA: {capt.nivel_buscado_actual.value}")
    if capt.miedos:
        lines.append(f"- Miedos detectados: {', '.join(capt.miedos)}")
    if capt.resono_con:
        lines.append(f"- Le resonó: {', '.join(capt.resono_con)}")
    if capt.objeciones_planteadas:
        lines.append(f"- Objeciones planteadas: {', '.join(capt.objeciones_planteadas)}")
    if capt.pidio_costos:
        niveles = ", ".join(n.value for n in capt.costos_compartidos_niveles) or "sí"
        lines.append(f"- Ya pidió costos: {niveles}")
    if capt.cita_agendada:
        when = capt.fecha_cita.isoformat() if capt.fecha_cita else "sí"
        campus = capt.campus_cita or "?"
        lines.append(f"- Cita YA agendada: {when} en {campus}")
    if capt.vive_fuera_saltillo:
        lines.append("- Vive fuera de Saltillo (ofrecer video llamada)")
    if capt.fuente_entrada:
        lines.append(f"- Fuente de entrada: {capt.fuente_entrada}")

    if estado.frases_usadas:
        frases_str = "; ".join(f'"{f}"' for f in estado.frases_usadas[-5:])
        lines.append(f"- Frases de munición YA usadas en este chat (no las repitas): {frases_str}")

    if not lines:
        return None

    return (
        "# ESTADO YA CAPTURADO DEL PAPÁ\n\n"
        "Esto es lo que YA sabes de este papá por mensajes previos. **No vuelvas a preguntar lo que ya está aquí.**\n\n"
        + "\n".join(lines)
    )


def _tabla_proximos_dias(now: datetime | None = None) -> str:
    """Tabla pre-calculada de hoy + próximos 7 días (FIX 1 — 2026-05-29).

    Red de respaldo determinística: Haiku calculaba mal "el lunes = 2 de junio"
    cuando el lunes era el 1. Le damos la equivalencia día→fecha ya resuelta
    para que NUNCA haga aritmética de calendario por su cuenta.
    """
    dt = now or datetime.now(TZ_MONTERREY)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MONTERREY)
    else:
        dt = dt.astimezone(TZ_MONTERREY)

    lineas: list[str] = []
    for i in range(8):
        d = dt + timedelta(days=i)
        if i == 0:
            etiqueta = "hoy"
        elif i == 1:
            etiqueta = "mañana"
        else:
            etiqueta = _DIAS_ES[d.weekday()]
        fecha_txt = f"{_DIAS_ES[d.weekday()]} {d.day} de {_MESES_ES[d.month - 1]}"
        lineas.append(f"    - {etiqueta} = {fecha_txt}")
    return "\n".join(lineas)


def _meta_block(estado: EstadoConversacion, *, now: datetime | None = None) -> str:
    """Bloque dinámico con metadata del turno (canal, modo, fase, fecha actual)."""
    return (
        f"# CONTEXTO DEL TURNO\n\n"
        f"- **Hoy es {_hoy_humano(now)}** (zona horaria America/Monterrey).\n"
        f"  Cuando hables de un día (de cita, de reunión, etc.), **siempre** acompáñalo\n"
        f"  de la fecha exacta. **NO calcules la fecha tú: úsala de esta tabla ya resuelta.**\n"
        f"  Equivalencia día → fecha (próximos 7 días):\n"
        f"{_tabla_proximos_dias(now)}\n"
        f"  Ejemplo: si el papá dice 'el lunes', escribe el 'lunes' con la fecha EXACTA de\n"
        f"  la tabla, nunca solo 'lunes' ni una fecha inventada.\n"
        f"- Canal: **{estado.canal.value}**\n"
        f"- Fase actual del journey: **{estado.fase_journey.value}**\n"
        f"- Modo: **{estado.modo.value}**\n"
        f"- Cita ya agendada: **{'sí' if estado.agendado else 'no'}**\n"
    )


def build_system_blocks(estado: EstadoConversacion) -> list[dict[str, Any]]:
    """Compone el system prompt como lista de bloques para Anthropic Messages API.

    Estructura (en este orden):
    1. **identity.md** — cacheable
    2. **rules.md** — cacheable
    3. **vocabulario.md** — cacheable
    4. **journey/<fase>.md** — cacheable (cambia por sesión, no por turno)
    5. **modo_aprendizaje.md** / **post_agendado.md** — solo si aplica, SIN cache
    6. *Bloque dinámico*: meta del turno + estado capturado — NO cacheable

    Límite de Anthropic: **máximo 4 bloques con cache_control**. Por eso los
    bloques opcionales (modo, post_agendado) van sin cache. Si esos crecen mucho
    en frecuencia, considerar consolidarlos en `identity.md`.
    """
    settings = get_settings()
    cacheable = bool(settings.enable_prompt_caching)
    blocks: list[dict[str, Any]] = []

    # 1-2. identity + rules (cacheables)
    blocks.append(_text_block(load_prompt_file("identity.md"), cacheable=cacheable))
    blocks.append(_text_block(load_prompt_file("rules.md"), cacheable=cacheable))

    # 3. BASE DE CONOCIMIENTO OFICIAL (cacheable — fuente de verdad de Sofía).
    # Es lo que cierra el "no apego": Sofía responde leyendo el documento oficial,
    # no la paráfrasis congelada de los _BEATS.
    kb_text = load_kb_oficial()
    if kb_text:
        blocks.append(_text_block(_KB_OFICIAL_HEADER + kb_text, cacheable=cacheable))

    # vocabulario — ahora SIN cache. El KB oficial ya cubre tono/argot/prohibiciones,
    # y Anthropic permite máx 4 bloques cacheables (identity, rules, KB, journey).
    blocks.append(_text_block(load_prompt_file("vocabulario.md"), cacheable=False))

    # 4. Journey de la fase activa (cacheable — 4to y último bloque con cache)
    journey_file = _JOURNEY_FILES.get(estado.fase_journey)
    if journey_file:
        blocks.append(_text_block(load_prompt_file(journey_file), cacheable=cacheable))

    # 5. Bloques opcionales — SIN cache (ya tenemos 4 cacheados, Anthropic permite max 4)
    if estado.agendado and estado.fase_journey != FaseJourney.POST_AGENDADO:
        blocks.append(_text_block(load_prompt_file("journey/post_agendado.md"), cacheable=False))

    if estado.modo == Modo.APRENDIZAJE:
        blocks.append(_text_block(load_prompt_file("modo_aprendizaje.md"), cacheable=False))

    # 6. Bloque dinámico — meta + estado capturado (NO cacheable)
    dynamic_parts = [_meta_block(estado)]
    capt_block = _datos_capturados_block(estado)
    if capt_block:
        dynamic_parts.append(capt_block)
    blocks.append(_text_block("\n\n---\n\n".join(dynamic_parts), cacheable=False))

    return blocks


def _text_block(text: str, cacheable: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cacheable:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def estimate_total_tokens(blocks: list[dict[str, Any]]) -> int:
    """Estimación rough: ~4 chars/token. Útil para logging/observabilidad."""
    total_chars = sum(len(b.get("text", "")) for b in blocks)
    return total_chars // 4
