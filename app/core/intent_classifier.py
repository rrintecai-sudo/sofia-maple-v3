"""Clasificador de intención del mensaje del usuario.

Usa gpt-4o-mini con structured output. La intención clasificada guía al
orchestrator (fase del journey, qué tools considerar).
"""

from __future__ import annotations

import json
import logging
import re
from enum import StrEnum

from pydantic import BaseModel, Field

from app.adapters.openai_client import get_openai

log = logging.getLogger(__name__)


class Intent(StrEnum):
    SALUDO_INICIAL = "saludo_inicial"
    PREGUNTA_COSTOS = "pregunta_costos"
    PREGUNTA_HORARIO = "pregunta_horario"
    PREGUNTA_NIVEL = "pregunta_nivel"
    PREGUNTA_METODOLOGIA = "pregunta_metodologia"
    PREGUNTA_PROCESO_ADMISION = "pregunta_proceso_admision"
    PREGUNTA_ESTANCIAS = "pregunta_estancias"
    PREGUNTA_BECAS = "pregunta_becas"
    PREGUNTA_CAMPUS = "pregunta_campus"
    PREGUNTA_PREPA = "pregunta_prepa"
    PREGUNTA_GENERAL_MAPLE = "pregunta_general_maple"
    QUIERE_AGENDAR = "quiere_agendar"
    MENCIONA_DIAGNOSTICO = "menciona_diagnostico"
    OBJECION_CARO = "objecion_caro"
    OBJECION_FLEXIBLE = "objecion_flexible"
    OBJECION_TAREA = "objecion_tarea"
    OBJECION_OTRA = "objecion_otra"
    DESPEDIDA = "despedida"
    RESPUESTA_CORTA_AL_TURNO_PREVIO = "respuesta_corta_al_turno_previo"
    CONFUSO_OTRO = "confuso_otro"


class IntentResult(BaseModel):
    """Resultado de la clasificación."""

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    razonamiento_breve: str | None = None


_SYSTEM_PROMPT = """Eres un clasificador de intención para Sofía, agente de admisiones de Maple Collège.

Recibes un mensaje del usuario (papá/mamá interesado en el colegio) y devuelves la intención dominante en formato JSON.

Categorías disponibles:
- saludo_inicial: hola, buen día, primer contacto
- pregunta_costos: cuánto cuesta, precios, colegiatura, mensualidad
- pregunta_horario: a qué hora, horario, qué hora entran/salen
- pregunta_nivel: quiero info de kinder/primaria/etc., qué niveles tienen
- pregunta_metodologia: qué método usan, cómo enseñan, qué es PBL/BEAR
- pregunta_proceso_admision: cuál es el proceso, cómo inscribo
- pregunta_estancias: estancia, after school, jornada extendida
- pregunta_becas: descuentos, becas, apoyo económico
- pregunta_campus: dónde están, dirección, ubicación
- pregunta_prepa: preparatoria, bachillerato
- pregunta_general_maple: cualquier pregunta general sobre el colegio
- quiere_agendar: el papá expresa querer visitar/conocer el colegio o agendar/reagendar. Incluye ejemplos: "quiero agendar una visita", "sí quiero conocer Maple", "agenda para el martes", "puedo ir el lunes 10am", "cuándo puedo visitar", "me parece bien la visita", "agéndame", "podemos vernos el viernes", "cuándo podríamos pasar a verlos". NO uses esta categoría si el papá pregunta por **horarios escolares** del colegio (eso es pregunta_horario) ni por el **proceso de admisión** sin pedir cita (eso es pregunta_proceso_admision).
- menciona_diagnostico: autismo, TDAH, diagnóstico, neurodivergente
- objecion_caro: está caro, es mucho, no me alcanza
- objecion_flexible: no hay disciplina, muy flexible, sin estructura
- objecion_tarea: no dejan tarea, quiero que le dejen tarea
- objecion_otra: otra duda/objeción
- despedida: adiós, gracias, hasta luego
- respuesta_corta_al_turno_previo: el papá responde con un mensaje muy corto (≤15 caracteres después de quitar espacios) que es una **continuación o confirmación** del turno anterior tuyo. Ejemplos: "sí", "no", "ok", "listo", "claro", "5to", "primaria", "kinder", "9 años", "que más", "cuéntame más", "y luego", "ajá". **NO uses esta categoría si es saludo inicial nuevo** (ej. "hola" sin contexto previo). El runtime solo aplica esta categoría si ya hay turno previo de Sofía en el historial.
- confuso_otro: no se puede clasificar

Devuelve EXCLUSIVAMENTE JSON con esta estructura:
{"intent": "<categoria>", "confidence": 0.0-1.0, "razonamiento_breve": "opcional, máximo 1 oración"}
"""


async def classify_intent(
    message: str,
    *,
    historial_reciente: list[str] | None = None,
    hay_turno_previo_assistant: bool = False,
) -> IntentResult:
    """Clasifica la intención de un mensaje.

    Args:
        message: el mensaje del usuario a clasificar.
        historial_reciente: últimos N mensajes (con prefijo de rol opcional)
            para que el LLM tenga contexto al desambiguar mensajes ambiguos
            (ej. "interactuara y que aprenda" → es respuesta de descubrimiento,
            no saludo inicial). Se inyectan en el user_text.
        hay_turno_previo_assistant: si True, el guard post-LLM **fuerza override**
            a `CONFUSO_OTRO` si el LLM devolvió `SALUDO_INICIAL`. Bug fix del
            hotfix post-5.7: previene que Sofía se vuelva a presentar a mitad
            de conversación cuando el classifier se confunde con mensajes
            ambiguos del papá.

    Returns:
        IntentResult con intent, confidence y razonamiento_breve.

    Raises:
        Para errores de API o JSON inválido, retorna `Intent.CONFUSO_OTRO` con confidence baja
        y loggea el error — NO levanta excepción (resiliencia).
    """
    openai = get_openai()
    if not openai.is_configured():
        log.warning("openai not configured, returning confuso_otro")
        return IntentResult(intent=Intent.CONFUSO_OTRO, confidence=0.0)

    user_text = message
    if historial_reciente:
        contexto = "\n".join(f"- {m}" for m in historial_reciente[-5:])
        user_text = f"Contexto reciente:\n{contexto}\n\nMensaje a clasificar:\n{message}"

    try:
        raw = await openai.classify(
            text=user_text,
            instructions=_SYSTEM_PROMPT,
        )
    except Exception as exc:
        log.warning("intent_classifier api error", extra={"error": str(exc)})
        return IntentResult(intent=Intent.CONFUSO_OTRO, confidence=0.0)

    result = _parse_result(raw)

    # Hotfix post-5.7: guard duro contra saludo_inicial cuando ya hay historial.
    # Si el LLM se confunde y clasifica una respuesta de descubrimiento como
    # saludo, override a CONFUSO_OTRO. El _decidir_fase del orchestrator
    # tratará CONFUSO_OTRO como "no cambia fase" — la conversación NO retrocede
    # a BIENVENIDA, y Sofía NO se vuelve a presentar.
    if result.intent == Intent.SALUDO_INICIAL and hay_turno_previo_assistant:
        log.warning(
            "intent_override saludo_inicial→confuso_otro por historial: %r",
            message[:80],
        )
        return IntentResult(
            intent=Intent.CONFUSO_OTRO,
            confidence=result.confidence,
            razonamiento_breve=(
                f"override hotfix: LLM marcó saludo_inicial pero hay turno previo. "
                f"original: {result.razonamiento_breve or ''}"
            )[:200],
        )

    return result


# Bloque 5.7 ATAQUE 2 — detector heurístico de "respuesta corta al turno previo".
# Usado como gate post-classifier: si el LLM marca otro intent pero la heurística
# detecta este patrón Y hay turno previo de Sofía, sobreescribimos.
_RESPUESTA_CORTA_KEYWORDS = re.compile(
    r"^\s*(?:s[ií]\s*(?:por\s*favor)?|no(?:\s*gracias)?|ok|okay|okey|listo|claro|"
    r"aja|aj[aá]|de\s*acuerdo|exacto|exactamente|"
    r"que\s+m[aá]s\??|cu[eé]ntame(?:\s+m[aá]s)?|sigue|y\??|y\s+luego|"
    r"\d{1,2}\s*(?:°|to|do|ro|er|vo|no|mo|cuarto|quinto|sexto)?(?:\s+(?:de\s+)?(?:primaria|secundaria|kinder|maternal))?|"
    r"\d{1,2}\s*a[ñn]os?|\d{1,2}\s*meses?|"
    # Ordinales escritos (hotfix post-debug correction_lost):
    r"(?:primer|primero|segundo|tercer|tercero|cuarto|quinto|sexto|s[eé]ptimo|octavo|noveno|d[eé]cimo)"
    r"(?:\s+(?:grado|de)(?:\s+(?:primaria|secundaria|kinder))?)?|"
    r"primaria|secundaria|kinder|maternal|preescolar|"
    r"infants|baby|cubs|toddlers|preschool)\s*[\.\?\!]?\s*$",
    re.IGNORECASE,
)


# FIX (2026-06-02): trigger DETERMINÍSTICO de "quiere agendar". El clasificador
# LLM no debe ser load-bearing: clasificaba "ahora quiero agendar otra para mi
# hija" como confuso_otro → el re-armado no disparaba → ghost-close. Esta regex
# de respaldo detecta la intención de agendar aunque el LLM falle.
_QUIERE_AGENDAR_RE = re.compile(
    r"\b(?:quiero|quisiera|queremos|quer[íi]a|puedo|podemos|me\s+gustar[íi]a)\s+agendar\b"
    r"|\bagendar\s+(?:una|otra)\b"
    r"|\bag[ée]nda(?:me|nos)?\b"
    r"|\bquiero\s+(?:otra|una)\s+(?:cita|visita)\b"
    r"|\b(?:agendar|reagendar)\s+(?:una\s+)?(?:cita|visita)\b"
    # Señales de VISITA = querer conocer/visitar el colegio → agendar (no discovery).
    # Requiere objeto-lugar para no atrapar "conocer los costos / más sobre X".
    r"|\b(?:conocer|visitar|ver)\s+(?:el\s+|la\s+|las\s+|los\s+|sus\s+|su\s+)?"
    r"(?:colegio|escuela|maple|instalaciones|plantel|campus|k[íi]nder|primaria|secundaria)\b"
    r"|\b(?:conocer|visitar|ver)(?:los|las)\b"
    r"|\b(?:pasar|ir)\s+a\s+(?:ver|conocer|visitar)(?:los|las)?\b"
    r"|\bcu[áa]ndo\s+(?:puedo|podemos|podr[íi]a(?:mos)?|se\s+puede)\s+"
    r"(?:visitar|conocer|pasar|ir|ver)\b",
    re.IGNORECASE,
)


def quiere_agendar_explicito(mensaje: str) -> bool:
    """True si el mensaje EXPRESA agendar de forma explícita ('quiero agendar',
    'agendar otra', 'agéndame'). Respaldo determinístico del clasificador LLM."""
    return bool(_QUIERE_AGENDAR_RE.search(mensaje or ""))


# Pedir INFO (informes/costos/horarios) = EXPLORACIÓN, NO agendar. El clasificador
# LLM mete "quiero informes" a QUIERE_AGENDAR porque la cita se llama "cita de
# informes" → este guard evita entrar a AGENDANDO por una consulta de información.
_INFO_EXPLORATORIA_RE = re.compile(
    r"\b(?:informes?|informaci[óo]n|costos?|precios?|colegiaturas?|mensualidad(?:es)?|"
    r"horarios?|cu[áa]nto\s+(?:cuesta|sale|es)|estancias?)\b",
    re.IGNORECASE,
)


def menciona_info_exploratoria(mensaje: str) -> bool:
    """True si el mensaje pide INFO (informes/costos/horarios/estancias) — señal de
    EXPLORACIÓN, no de agendar. Úsalo para que el clasificador LLM no dispare el
    agendado por 'quiero informes'."""
    return bool(_INFO_EXPLORATORIA_RE.search(mensaje or ""))


# El papá pide hablar con una persona / no quiere un "robot". NUNCA revelar que es IA;
# responder cálido y reconducir (sin menú robótico).
_QUIERE_PERSONA_RE = re.compile(
    r"\bhablar\s+con\s+(?:una\s+|otra\s+|alguna\s+)?(?:persona|alguien|humano|asesor|"
    r"ejecutiv|agente|encargad|admisiones)\b"
    r"|\bno\s+(?:quiero|me\s+gusta|es)\s+(?:un\s+)?(?:robot|bot|m[áa]quina|chatbot|ia)\b"
    r"|\b(?:eres|es)\s+(?:un\s+)?(?:robot|bot|m[áa]quina|chatbot|ia|inteligencia)\b"
    r"|\bcon\s+(?:un\s+|una\s+)?(?:humano|persona\s+real|ser\s+humano)\b"
    r"|\bme\s+est[áa]s?\s+(?:contestando|atendiendo)\s+(?:un\s+)?(?:robot|bot|m[áa]quina)\b",
    re.IGNORECASE,
)


def quiere_persona_humana(mensaje: str) -> bool:
    """True si el papá pide hablar con una persona o rechaza al 'robot'."""
    return bool(_QUIERE_PERSONA_RE.search(mensaje or ""))


def es_respuesta_corta_al_turno_previo(mensaje: str, hay_turno_previo_assistant: bool) -> bool:
    """Heurística determinística (Bloque 5.7 ATAQUE 2).

    Devuelve True si el mensaje cumple:
      - ≤22 caracteres después de trim (cubre "segundo de primaria", etc.)
      - Encaja en patrones confirmatorios/numéricos/continuación/ordinales
      - HAY turno previo del assistant en el historial (guard A)

    Si NO hay turno previo, el intent NO aplica (sería saludo inicial).
    """
    if not hay_turno_previo_assistant:
        return False
    msg = mensaje.strip()
    if len(msg) == 0 or len(msg) > 22:
        return False
    return bool(_RESPUESTA_CORTA_KEYWORDS.match(msg))


def _parse_result(raw: str) -> IntentResult:
    """Parse defensive de la respuesta JSON del modelo."""
    # gpt-4o-mini a veces devuelve JSON con backticks ```json ... ```
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning(
            "intent_classifier non-json response", extra={"raw": raw[:200], "err": str(exc)}
        )
        return IntentResult(intent=Intent.CONFUSO_OTRO, confidence=0.0)

    try:
        return IntentResult.model_validate(data)
    except Exception as exc:  # pydantic validation
        log.warning(
            "intent_classifier invalid schema",
            extra={"data": data, "err": str(exc)},
        )
        return IntentResult(intent=Intent.CONFUSO_OTRO, confidence=0.0)
