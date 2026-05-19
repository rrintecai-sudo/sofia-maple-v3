"""Validators determinísticos para post-procesamiento de respuestas de Sofía.

Cada validator es una función pura: recibe la respuesta del LLM + contexto,
devuelve un `ValidationResult`. Si alguno falla y aún hay budget de regeneración,
el orchestrator reintenta inyectando feedback al prompt.

Ver ARCHITECTURE §7 y SOFIA_BUILD_PLAN Bloque 3 Paso 3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.core.intent_classifier import Intent
from app.core.state import EstadoCapturado

# ============================================================
# Frases munición — extraídas literal de vocabulario.md
# Detecta por substring case-insensitive — el modelo a veces parafrasea
# levemente, pero la columna vertebral de la frase queda.
# ============================================================
FRASES_MUNICION: tuple[str, ...] = (
    "hay escuelas caras y hay escuelas valiosas",
    "los primeros años no se repiten",
    "maple collège no es para todos",
    "no entrenamos niños para obedecer",
    "el mundo ya cambió",
    "no elegimos alumnos. nos elegimos mutuamente",
    "una educación así, bien hecha, no puede ser barata",
    "el precio solo duele cuando el valor no está claro",
    "te conviertes en parte del proceso",
    "no quitamos el juego, le damos intención",
    "no quitamos la exigencia, la hacemos sostenible",
    "tu hijo no solo aprende… se forma",
    "que tu hijo pueda sostener lo que aprende en la vida",
    # Siembra de alianza escuela-familia
    "trabajamos muy de la mano con las familias",
    "el desarrollo no pasa solo en el salón",
)

# Patrones que sugieren envío de algo (imagen, sticker, archivo)
_ENVIO_PATTERNS = (
    r"\bya te env[ií][eé]\b",
    r"\bte env[ií][eé]\b",
    r"\bte adjunto\b",
    r"\bte mand[oé]\b",
    r"\bte acabo de enviar\b",
    r"\bte paso la imagen\b",
    r"\bte paso la tabla\b",
    r"\bya te compart[ií]\b",
    r"\bya te mostr[eé]\b",
)
_ENVIO_REGEX = re.compile("|".join(_ENVIO_PATTERNS), re.IGNORECASE)

# Patrones que indican que la respuesta pregunta por un dato YA conocido.
# Cada patrón se asocia a un campo de EstadoCapturado.
_PREGUNTA_NIVEL_RE = re.compile(
    r"(?:para\s+)?qu[eé]\s+nivel|qu[eé]\s+(?:grado|etapa)\s+(?:est[aá]|va|busc)|"
    r"en\s+qu[eé]\s+(?:nivel|etapa|grado)|qu[eé]\s+est[aá]s\s+buscando|"
    r"\bbuscas\s+(?:para\s+)?qu[eé]\s+nivel",
    re.IGNORECASE,
)
_PREGUNTA_NOMBRE_HIJO_RE = re.compile(
    r"c[oó]mo\s+se\s+llama\s+tu\s+hijo|"
    r"cu[aá]l\s+es\s+el\s+nombre\s+de\s+tu\s+hijo|"
    r"el\s+nombre\s+de\s+tu\s+(?:peque|hijo|hija)",
    re.IGNORECASE,
)
_PREGUNTA_EDAD_RE = re.compile(
    r"qu[eé]\s+edad\s+tiene|cu[aá]ntos\s+a[ñn]os\s+tiene\s+tu\s+(?:hijo|hija|peque)",
    re.IGNORECASE,
)
_PREGUNTA_ESCUELA_ACTUAL_RE = re.compile(
    r"est[aá]\s+(?:ahorita\s+)?en\s+alguna\s+escuela|"
    r"\ben\s+qu[eé]\s+escuela\s+est[aá]|"
    r"tiene\s+escuela\s+actualmente|"
    r"va\s+a\s+alguna\s+escuela\s+ahorita",
    re.IGNORECASE,
)

# Patrón para detectar números (precios) en una respuesta
_NUMERO_RE = re.compile(
    r"\$?\s*\d[\d,.\s]*\d|\d+\s*(?:pesos|mxn|colegiatura|inscripción|al\s+mes|mensuales)",
    re.IGNORECASE,
)
_DEJAME_CONFIRMAR_RE = re.compile(
    r"d[eé]jame\s+confirmar|consult(?:o|a)\s+(?:con\s+)?el\s+equipo|"
    r"te\s+respondo\s+a\s+la\s+brevedad|no\s+tengo\s+ese\s+dato",
    re.IGNORECASE,
)


# ============================================================
# Resultado de validación
# ============================================================


@dataclass(frozen=True)
class ValidationResult:
    """Resultado individual de un validator."""

    validator: str
    passed: bool
    reason: str | None = None  # mensaje legible si falla
    suggested_fix: str | None = None  # instrucción para inyectar al prompt en regeneración
    severity: Literal["error", "warning"] = "error"


@dataclass
class ValidationReport:
    """Agregado de todos los validators corridos en un turno."""

    results: list[ValidationResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results if r.severity == "error")

    @property
    def failed(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed]

    @property
    def passed_map(self) -> dict[str, bool]:
        """Mapa para persistir en `sofia_turn_logs.validators_passed`."""
        return {r.validator: r.passed for r in self.results}

    @property
    def failed_map(self) -> dict[str, str]:
        """Mapa para `sofia_turn_logs.validators_failed`."""
        return {r.validator: r.reason or "failed" for r in self.results if not r.passed}

    def feedback_para_regenerar(self) -> str | None:
        """Construye el texto que se inyecta al prompt para que el modelo regenere."""
        fails = self.failed
        if not fails:
            return None
        lines = ["Tu respuesta anterior tuvo estos problemas que DEBES corregir:"]
        for r in fails:
            tip = r.suggested_fix or r.reason or "ajusta tu respuesta"
            lines.append(f"- {r.validator}: {tip}")
        lines.append(
            "Genera de nuevo aplicando estas correcciones, sin disculparte ni mencionar el ajuste al usuario."
        )
        return "\n".join(lines)


# ============================================================
# Validators individuales
# ============================================================


def validar_no_repeticion(respuesta: str, frases_usadas: list[str]) -> ValidationResult:
    """Falla si la respuesta contiene una frase de munición que ya se usó en este chat.

    Las "frases munición" están definidas en `FRASES_MUNICION` (subset clave de las
    13 frases del prompt + las 2 variantes de la siembra de alianza).
    """
    resp_lower = respuesta.lower()
    usadas_lower = {f.lower() for f in frases_usadas}

    for frase in FRASES_MUNICION:
        if frase in resp_lower and any(frase in u for u in usadas_lower):
            return ValidationResult(
                validator="no_repeticion",
                passed=False,
                reason=f'Frase munición ya usada: "{frase[:60]}…"',
                suggested_fix=(
                    f'Evita repetir la frase "{frase[:60]}…" — ya la usaste antes en este chat. '
                    f"Comunica la idea con otras palabras o pasa a otro punto."
                ),
            )
    return ValidationResult(validator="no_repeticion", passed=True)


def validar_no_envio_fantasma(
    respuesta: str,
    tools_called: list[str] | None = None,
) -> ValidationResult:
    """Falla si la respuesta afirma haber enviado algo sin que se haya llamado el tool.

    Detecta patrones tipo "ya te envié", "te adjunto", "te mandé la imagen", etc.
    Si la respuesta los menciona pero `tools_called` NO incluye un tool de envío
    (`send_image`, `send_sticker`), falla.
    """
    tools_called = tools_called or []
    tools_envio = {
        "send_image",
        "send_sticker",
        "send_image_costos_kinder",
        "send_sticker_despedida",
    }

    match = _ENVIO_REGEX.search(respuesta)
    if not match:
        return ValidationResult(validator="no_envio_fantasma", passed=True)

    if any(t in tools_envio for t in tools_called):
        return ValidationResult(validator="no_envio_fantasma", passed=True)

    return ValidationResult(
        validator="no_envio_fantasma",
        passed=False,
        reason=f'Afirma envío sin tool: "...{match.group(0)}..."',
        suggested_fix=(
            "NO afirmes que enviaste imagen, archivo, sticker, link ni nada — "
            "NO se llamó a ninguna tool de envío. Elimina cualquier frase tipo "
            '"ya te envié", "te adjunto", "te mandé la imagen". Si necesitas '
            "compartir información, dala en texto."
        ),
    )


def validar_no_pregunta_repetida(
    respuesta: str,
    estado: EstadoCapturado,
) -> ValidationResult:
    """Falla si la respuesta pregunta algo que ya está en el estado capturado."""
    # Nivel ya conocido (en el estado actual o algún hijo lo tiene)
    nivel_conocido = estado.nivel_buscado_actual is not None or any(
        h.nivel is not None for h in estado.hijos
    )
    if nivel_conocido and _PREGUNTA_NIVEL_RE.search(respuesta):
        nivel_val = (
            estado.nivel_buscado_actual.value
            if estado.nivel_buscado_actual
            else next((h.nivel.value for h in estado.hijos if h.nivel), "?")
        )
        return ValidationResult(
            validator="no_pregunta_repetida",
            passed=False,
            reason=f"Pregunta por nivel cuando ya sabe que es {nivel_val}",
            suggested_fix=(
                f"NO preguntes el nivel — el papá ya te dijo que es {nivel_val}. "
                "Usa esa información directamente."
            ),
        )

    # Nombre del hijo
    nombre_conocido = any(h.nombre for h in estado.hijos)
    if nombre_conocido and _PREGUNTA_NOMBRE_HIJO_RE.search(respuesta):
        nombres = [h.nombre for h in estado.hijos if h.nombre]
        return ValidationResult(
            validator="no_pregunta_repetida",
            passed=False,
            reason=f"Pregunta nombre del hijo cuando ya lo sabe: {nombres}",
            suggested_fix=f"NO preguntes el nombre del hijo — ya lo sabes: {', '.join(nombres)}.",
        )

    # Edad del hijo
    edad_conocida = any(h.edad is not None for h in estado.hijos)
    if edad_conocida and _PREGUNTA_EDAD_RE.search(respuesta):
        edades = [str(h.edad) for h in estado.hijos if h.edad is not None]
        return ValidationResult(
            validator="no_pregunta_repetida",
            passed=False,
            reason=f"Pregunta edad cuando ya la sabe: {edades}",
            suggested_fix=f"NO preguntes la edad — ya la sabes ({', '.join(edades)} años).",
        )

    # Escuela actual
    escuela_conocida = any(h.escuela_actual for h in estado.hijos)
    if escuela_conocida and _PREGUNTA_ESCUELA_ACTUAL_RE.search(respuesta):
        return ValidationResult(
            validator="no_pregunta_repetida",
            passed=False,
            reason="Pregunta escuela actual cuando ya sabe que sí está en una",
            suggested_fix=(
                "NO preguntes si está en alguna escuela — el papá ya te lo dijo. "
                "Si quieres más contexto pregunta algo diferente, no eso."
            ),
        )

    return ValidationResult(validator="no_pregunta_repetida", passed=True)


def validar_no_evasion(respuesta: str, intent: Intent | None) -> ValidationResult:
    """Falla si la pregunta era cerrada (costos/horarios) y la respuesta evade.

    Para pregunta_costos: la respuesta debe contener un número o "déjame confirmar".
    Para pregunta_horario: la respuesta debe mencionar un horario (formato H:MM) o
    pedir aclaración del nivel.
    """
    if intent is None:
        return ValidationResult(validator="no_evasion", passed=True)

    if intent == Intent.PREGUNTA_COSTOS:
        if _NUMERO_RE.search(respuesta) or _DEJAME_CONFIRMAR_RE.search(respuesta):
            return ValidationResult(validator="no_evasion", passed=True)
        # Excepción: si la respuesta pide aclarar el nivel, es válido
        if re.search(
            r"qu[eé]\s+nivel|para\s+qu[eé]\s+(?:nivel|grado|etapa)", respuesta, re.IGNORECASE
        ):
            return ValidationResult(validator="no_evasion", passed=True)
        return ValidationResult(
            validator="no_evasion",
            passed=False,
            reason="Pregunta de costos sin número, sin 'déjame confirmar' ni clarificación de nivel",
            suggested_fix=(
                "El papá preguntó costos directos. Tu primera oración debe responder con "
                "un monto exacto del nivel correspondiente o pedir explícitamente '¿para qué nivel?' "
                "si no lo sabes."
            ),
        )

    if intent == Intent.PREGUNTA_HORARIO:
        horario_re = re.compile(r"\d{1,2}:\d{2}|\d{1,2}\s+a(?:m|\.m)|\d{1,2}\s+pm", re.IGNORECASE)
        if horario_re.search(respuesta) or _DEJAME_CONFIRMAR_RE.search(respuesta):
            return ValidationResult(validator="no_evasion", passed=True)
        if re.search(
            r"qu[eé]\s+nivel|para\s+qu[eé]\s+(?:nivel|grado|etapa)", respuesta, re.IGNORECASE
        ):
            return ValidationResult(validator="no_evasion", passed=True)
        return ValidationResult(
            validator="no_evasion",
            passed=False,
            reason="Pregunta de horario sin hora concreta ni aclaración de nivel",
            suggested_fix=(
                "El papá preguntó horarios. Da las horas concretas del nivel "
                "o pregúntale '¿para qué nivel?' si no lo tienes."
            ),
        )

    return ValidationResult(validator="no_evasion", passed=True)


# ============================================================
# Runner — ejecuta todos los validators
# ============================================================


def run_all_validators(
    respuesta: str,
    estado: EstadoCapturado,
    intent: Intent | None = None,
    tools_called: list[str] | None = None,
    frases_usadas: list[str] | None = None,
) -> ValidationReport:
    """Ejecuta los 4 validators secuencialmente y agrega resultados.

    Es pura: no escribe DB, no llama APIs. Solo razona sobre el texto.
    """
    report = ValidationReport()
    report.results.append(validar_no_repeticion(respuesta, frases_usadas or []))
    report.results.append(validar_no_envio_fantasma(respuesta, tools_called))
    report.results.append(validar_no_pregunta_repetida(respuesta, estado))
    report.results.append(validar_no_evasion(respuesta, intent))
    return report


def extraer_frases_municion_usadas(respuesta: str) -> list[str]:
    """Devuelve qué frases munición aparecen en la respuesta (para registrar).

    Usado por el orchestrator para añadir a `estado.frases_usadas` después de
    aceptar la respuesta.
    """
    resp_lower = respuesta.lower()
    return [frase for frase in FRASES_MUNICION if frase in resp_lower]


def _is_pregunta_cerrada_costos(intent: Intent | None) -> bool:
    """Helper público para diagnosticar — no se usa internamente."""
    return intent == Intent.PREGUNTA_COSTOS
