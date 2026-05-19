"""Detector de "momentos íntimos" en la conversación.

Un momento íntimo es cuando el papá:
- Habla de su hijo en términos emocionales (nombre, edad, miedos, sueños, dificultades)
- Comparte una historia personal o familiar
- Hace una pregunta corta de seguimiento (Si, Ok, Como, Que más) tras conversación
  emocional
- Expresa dudas profundas, miedos sobre el desarrollo del hijo, o emociones
  (preocupación, esperanza, cansancio)
- Pide aclaración después de un momento de vulnerabilidad

En esos momentos, Sofía debe responder en PROSA CÁLIDA, no con bullets/listas.
El detector es heurístico (keywords + estado) con fallback opcional a GPT-4o-mini
para casos ambiguos.

Output:
    IntimacyResult(es_intimo: bool, razon: str, confianza: float)

Diseño:
- Determinístico por default (rápido, sin API call extra)
- LLM fallback opcional (modelo auxiliar) cuando el caso es ambiguo
- Fallback graceful: si el LLM falla, asumimos `es_intimo=False`
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.core.state import EstadoConversacion, FaseJourney

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntimacyResult:
    es_intimo: bool
    razon: str
    confianza: float  # 0.0 a 1.0


# Palabras/frases que sugieren registro emocional / personal.
# Preferimos frases sobre palabras sueltas para reducir falsos positivos.
# Ej: "cuesta" sola matchearía "¿cuánto cuesta?" (operativo) — usamos "le cuesta".
_EMOTIONAL_KEYWORDS = (
    "miedo",
    "miedos",
    "preocupa",
    "preocupación",
    "preocupado",
    "preocupada",
    "ansiedad",
    "angustia",
    "le cuesta",
    "le costaba",
    "me cuesta",
    "tímido",
    "tímida",
    "introvertido",
    "extrovertido",
    "sensible",
    "frágil",
    "diagnóstico",
    "tdah",
    "autismo",
    "neurodivergente",
    "necesidades especiales",
    "espectro",
    "asperger",
    "berrinche",
    "berrinches",
    "rabieta",
    "lloro",
    "lloraba",
    "llorando",
    "no quiere",
    "no quería",
    "se aísla",
    "no socializa",
    "mi vida",
    "mi historia",
    "cuando yo",
    "yo crecí",
    "yo no quiero",
    "para que él",
    "para que ella",
    "que mi hijo no",
    "que mi hija no",
)

# Frases que indican una historia personal larga (no operativa)
_PERSONAL_NARRATIVE_PATTERNS = re.compile(
    r"\b(?:yo|nosotros)\s+(?:siempre|nunca|no\s+quiero|no\s+queremos|pasé|pasamos|"
    r"vivimos|crecimos|estuve|estuvimos|crecí|sufrí|sufrimos)\b",
    re.IGNORECASE,
)

# Patrones de preguntas/respuestas ultra cortas que en contexto íntimo SIGUEN siendo íntimas
_SHORT_FOLLOWUP_PATTERNS = re.compile(
    r"^\s*(?:s[ií]|ok|okay|aja|aj[aá]|claro|y\?|como\?|c[oó]mo\?|qu[eé]\s*m[aá]s\??|"
    r"cu[eé]ntame|sigue|y\s+luego|que\s+pas[oó]|q\s+m[aá]s)\s*\??\s*$",
    re.IGNORECASE,
)

# Patrón de pregunta operativa (claramente NO íntima)
_OPERATIONAL_PATTERNS = re.compile(
    r"\b(?:precio|precios|costo|costos|colegiatura|inscripci[oó]n|horario|horarios|"
    r"direcci[oó]n|ubicaci[oó]n|d[oó]nde|cu[aá]nto|cu[aá]ndo|fecha|cita|visita|"
    r"becas|descuento)\b",
    re.IGNORECASE,
)


def detectar_intimidad(
    mensaje_papa: str,
    estado: EstadoConversacion,
    *,
    historial_papa: list[str] | None = None,
) -> IntimacyResult:
    """Heurística determinística para detectar momentos íntimos.

    Reglas (en orden de prioridad):
    1. Mensaje operativo (precios, horarios, citas) → NO íntimo, conf alta.
    2. Mensaje con keywords emocionales o narrativa personal → SÍ íntimo, conf alta.
    3. Mensaje ultra corto + último mensaje del papá fue íntimo → SÍ íntimo, conf media.
    4. Fase journey == "descubrimiento" + mensaje >40 chars + no operativo → SÍ íntimo, conf media.
    5. Default → NO íntimo, conf baja.

    El caller puede usar `confianza < 0.6` para invocar LLM fallback si quiere.
    """
    msg = mensaje_papa.strip()
    msg_lower = msg.lower()

    # Regla 1: keywords emocionales o narrativa personal (gana sobre operativo
    # porque palabras como "cuando" pueden aparecer en frases emocionales
    # tipo "como yo cuando era niño").
    emotional_hits = [k for k in _EMOTIONAL_KEYWORDS if k in msg_lower]
    if emotional_hits or _PERSONAL_NARRATIVE_PATTERNS.search(msg_lower):
        return IntimacyResult(
            es_intimo=True,
            razon=f"Keywords emocionales: {emotional_hits[:3] or 'narrativa personal'}",
            confianza=0.9,
        )

    # Regla 2: operativo (sin señales emocionales)
    if _OPERATIONAL_PATTERNS.search(msg_lower):
        return IntimacyResult(
            es_intimo=False,
            razon="Mensaje operativo (precios/horarios/citas)",
            confianza=0.95,
        )

    # Regla 3: short followup + último mensaje del papá tenía señal emocional
    if _SHORT_FOLLOWUP_PATTERNS.match(msg):
        prev_msgs = historial_papa or []
        if prev_msgs:
            last_papa = prev_msgs[-1].lower()
            prev_emotional = any(k in last_papa for k in _EMOTIONAL_KEYWORDS)
            if prev_emotional:
                return IntimacyResult(
                    es_intimo=True,
                    razon="Short followup tras mensaje emocional previo",
                    confianza=0.7,
                )
        # Short followup en descubrimiento sin contexto emocional: ambiguo
        if estado.fase_journey == FaseJourney.DESCUBRIMIENTO:
            return IntimacyResult(
                es_intimo=True,
                razon="Short followup en fase descubrimiento",
                confianza=0.55,
            )

    # Regla 4: descubrimiento + mensaje sustancioso (>40 chars) y no operativo
    if estado.fase_journey == FaseJourney.DESCUBRIMIENTO and len(msg) > 40:
        return IntimacyResult(
            es_intimo=True,
            razon="Mensaje sustancioso en fase descubrimiento",
            confianza=0.7,
        )

    # Default
    return IntimacyResult(
        es_intimo=False,
        razon="Sin señales claras de intimidad emocional",
        confianza=0.4,
    )


async def detectar_intimidad_async(
    mensaje_papa: str,
    estado: EstadoConversacion,
    *,
    historial_papa: list[str] | None = None,
    use_llm_fallback: bool = False,
    threshold_confianza: float = 0.6,
) -> IntimacyResult:
    """Wrapper async para usar en `asyncio.gather` desde el orchestrator.

    Si `use_llm_fallback=True` y la heurística devuelve confianza < threshold,
    consulta GPT-4o-mini para refinar. Fallback graceful: si el LLM falla,
    devuelve el resultado heurístico original.
    """
    heuristic = detectar_intimidad(mensaje_papa, estado, historial_papa=historial_papa)

    if not use_llm_fallback or heuristic.confianza >= threshold_confianza:
        return heuristic

    try:
        from app.adapters.openai_client import get_openai

        openai = get_openai()
        if not openai.is_configured():
            return heuristic

        contexto_extra = ""
        if historial_papa:
            recent = "\n".join(f"- {m[:120]}" for m in historial_papa[-3:])
            contexto_extra = f"\n\nÚltimos mensajes del papá:\n{recent}"

        raw = await openai.classify(
            text=(
                f"Mensaje actual del papá: {mensaje_papa[:300]!r}\n"
                f"Fase del journey: {estado.fase_journey.value}{contexto_extra}\n\n"
                "¿Este es un MOMENTO ÍNTIMO de la conversación, donde Sofía debería "
                "responder en prosa cálida (NO bullets/listas)?\n"
                'Devuelve EXCLUSIVAMENTE JSON: {"es_intimo": true|false, "razon": "..."}'
            ),
            instructions=(
                "Eres un clasificador binario. Un momento es íntimo si el papá habla "
                "del hijo en términos emocionales/personales, comparte historia familiar, "
                "expresa miedo/duda profunda, o pregunta de seguimiento ('sí', 'ok', "
                "'cuéntame') tras una respuesta emocional. NO es íntimo si pregunta "
                "operativa (precios/horarios/citas). En empate, prefiere íntimo."
            ),
        )
        import json

        cleaned = (
            raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        data = json.loads(cleaned)
        es_intimo = bool(data.get("es_intimo"))
        razon = str(data.get("razon", ""))[:200]
        return IntimacyResult(
            es_intimo=es_intimo,
            razon=f"LLM: {razon}",
            confianza=0.85,
        )
    except Exception as exc:
        log.warning("intimacy_detector LLM fallback failed", extra={"error": str(exc)})
        return heuristic
