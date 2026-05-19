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
- quiere_agendar: quiero agendar, cita, visita, conocer el colegio
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
) -> IntentResult:
    """Clasifica la intención de un mensaje.

    Args:
        message: el mensaje del usuario a clasificar.
        historial_reciente: últimos N mensajes del usuario, para contexto opcional.

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

    return _parse_result(raw)


# Bloque 5.7 ATAQUE 2 — detector heurístico de "respuesta corta al turno previo".
# Usado como gate post-classifier: si el LLM marca otro intent pero la heurística
# detecta este patrón Y hay turno previo de Sofía, sobreescribimos.
_RESPUESTA_CORTA_KEYWORDS = re.compile(
    r"^\s*(?:s[ií]\s*(?:por\s*favor)?|no(?:\s*gracias)?|ok|okay|okey|listo|claro|"
    r"aja|aj[aá]|de\s*acuerdo|exacto|exactamente|"
    r"que\s+m[aá]s\??|cu[eé]ntame(?:\s+m[aá]s)?|sigue|y\??|y\s+luego|"
    r"\d{1,2}\s*(?:°|to|do|ro|er|vo|no|mo|cuarto|quinto|sexto)?(?:\s+(?:de\s+)?(?:primaria|secundaria|kinder|maternal))?|"
    r"\d{1,2}\s*a[ñn]os?|\d{1,2}\s*meses?|"
    r"primaria|secundaria|kinder|maternal|preescolar|"
    r"infants|baby|cubs|toddlers|preschool)\s*[\.\?\!]?\s*$",
    re.IGNORECASE,
)


def es_respuesta_corta_al_turno_previo(mensaje: str, hay_turno_previo_assistant: bool) -> bool:
    """Heurística determinística (Bloque 5.7 ATAQUE 2).

    Devuelve True si el mensaje cumple:
      - ≤15 caracteres después de trim
      - Encaja en patrones confirmatorios/numéricos/continuación
      - HAY turno previo del assistant en el historial (guard A)

    Si NO hay turno previo, el intent NO aplica (sería saludo inicial).
    """
    if not hay_turno_previo_assistant:
        return False
    msg = mensaje.strip()
    if len(msg) == 0 or len(msg) > 15:
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
