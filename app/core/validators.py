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
from app.core.state import EstadoCapturado, FaseJourney

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

# Patrones de markdown excesivo para WhatsApp/Telegram
_MARKDOWN_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)
_MARKDOWN_BOLD_RE = re.compile(r"\*\*[^*\n]+?\*\*")
_MARKDOWN_BULLET_RE = re.compile(r"^\s*[-•*]\s+\S", re.MULTILINE)
_MARKDOWN_NUMBERED_RE = re.compile(r"^\s*\d+[.\)]\s+\S", re.MULTILINE)
_DEJAME_CONFIRMAR_RE = re.compile(
    r"d[eé]jame\s+confirmar|consult(?:o|a)\s+(?:con\s+)?el\s+equipo|"
    r"te\s+respondo\s+a\s+la\s+brevedad|no\s+tengo\s+ese\s+dato",
    re.IGNORECASE,
)

# Bloque 5.7 ATAQUE 1 — patrones para validar_no_inventa_datos (severity=warning).
# Mismos regex que el 5.6 calibrado: detectan afirmaciones de datos no presentes
# en estado_capturado ni en mensajes_papa.
_AFIRMA_VIO_CONTENIDO_RE = re.compile(
    r"\bvi\s+(?:el|tu|la|los?|las?)\s+(?:link|enlace|imagen|video|contenido|post|publicaci[oó]n|art[ií]culo|p[aá]gina)\b|"
    r"\b(?:revis[eé]|le[íi]|mir[eé])\s+(?:el|tu|la|los?)\s+(?:link|enlace|contenido|post)\b|"
    r"\b(?:le[íi]|vi)\s+lo\s+que\s+(?:dice|me\s+enviaste|compart[ií]ste|compart[ií]aste)\b|"
    r"acabo\s+de\s+ver\s+(?:el|tu)",
    re.IGNORECASE,
)

# Afirmar nombre del papá ("Hola Juan, ...", "Mira Juan, ..."). Captura case-sensitive
# para evitar matchear muletillas ("qué", "claro").
_AFIRMA_NOMBRE_PAPA_RE = re.compile(
    r"(?:^|\.\s+|,\s+)(?:[Hh]ola|[Mm]ira|[Ff]íjate|[Oo]ye|[Cc]laro|[Ss][ií])[,]?\s+"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,15})\b"
)

_AFIRMA_NIVEL_HIJO_RE = re.compile(
    r"\btu\s+(?:hijo|hija|peque[ñn]o|peque[ñn]a|peque|ni[ñn]o|ni[ñn]a)\s+"
    r"(?:de|en|est[aá]\s+en|va\s+a|busca)\s+(maternal|kinder|preescolar|primaria|secundaria|"
    r"\d+\s*°\s*(?:de\s+)?(?:primaria|secundaria|kinder)|"
    r"infants|toddlers|cubs|baby|preschool)\b",
    re.IGNORECASE,
)

_AFIRMA_EDAD_HIJO_RE = re.compile(
    r"\btu\s+(?:hijo|hija|peque|ni[ñn]o|ni[ñn]a)\s+de\s+(\d{1,2})\s+(?:a[ñn]os?|meses)\b",
    re.IGNORECASE,
)

_AFIRMA_GENERO_HIJO_RE = re.compile(
    r"\btu\s+(hijo|hija)\b(?!\s*[oó]\s*(?:hija|hijo))",
    re.IGNORECASE,
)

_AFIRMA_CAMPUS_RE = re.compile(
    r"\b(?:en\s+|para\s+|al\s+|del?\s+|tu\s+(?:cita|visita)\s+(?:es\s+)?en\s+)"
    r"(campus\s*[12])\b",
    re.IGNORECASE,
)

_AFIRMA_CITA_AGENDADA_RE = re.compile(
    r"\b(?:ya\s+agendaste|tu\s+cita\s+(?:es|ser[aá]|qued[oó]|est[aá]\s+confirmada)|"
    r"tu\s+visita\s+(?:es|ser[aá]|qued[oó])|"
    r"te\s+espero\s+el\s+\w+|nos\s+vemos\s+el\s+\w+)",
    re.IGNORECASE,
)

# D.1 (feedback Gaby 2026-05-27): guiones largos (em-dash —) y guiones medios
# (en-dash –) en las respuestas de Sofía son señal de texto de IA. Bloqueamos
# ambos caracteres en cualquier parte del texto.
_GUION_LARGO_RE = re.compile(r"[—–]")


# FIX 4 (2026-05-29 — ADR-020): nombre del papá inventado, severity=error.
# Regex estricta: saludo vocativo + palabra capitalizada. Más conservadora que
# la versión warning para minimizar falsos positivos al subir a error (justo lo
# que ADR-017 evitó al bajar el bloque general). Combinada con una denylist.
_NOMBRE_VOCATIVO_RE = re.compile(
    r"(?:^|[.!?¡¿]\s+|,\s+)"
    r"(?:[Hh]ola|[Hh]ey|[Mm]ira|[Oo]ye|[Ff][íi]jate|[Pp]erfecto|[Cc]laro|[Gg]racias|"
    r"[Bb]ienvenid[oa]|[Bb]uen[oa]s)[,]?\s+"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,15})\b"
)

# Palabras que pueden seguir a un saludo SIN ser un nombre propio (evita
# falsos positivos como "Claro, Maple ofrece..." o "Gracias por tu mensaje").
_PALABRAS_NO_NOMBRE = frozenset(
    {
        "sof",
        "sofia",
        "sofía",
        "maple",
        "college",
        "collège",
        "colegio",
        "que",
        "qué",
        "como",
        "cómo",
        "claro",
        "mira",
        "oye",
        "hola",
        "hey",
        "gracias",
        "perfecto",
        "bienvenido",
        "bienvenida",
        "buenas",
        "buenos",
        "mucho",
        "muchas",
        "un",
        "una",
        "por",
        "para",
        "con",
        "cuando",
        "entonces",
        "ahora",
        "aqui",
        "aquí",
        "papa",
        "papá",
        "mama",
        "mamá",
        "genial",
        "excelente",
        "encantada",
        "estamos",
        "estoy",
        "este",
        "esta",
        "todo",
        "ya",
    }
)

# FIX 2/3 (2026-05-29 — ADR-021): confirmación de cita declarativa/completada.
# Si Sofía afirma haber registrado/confirmado/agendado una cita Y no existe
# appointment_id real, es una confirmación fantasma → bloqueo (severity=error).
_CONFIRMA_CITA_RE = re.compile(
    r"registr[ée]\s+(?:tu|su|la)\s+(?:solicitud|cita|visita)|"
    r"(?:ya\s+)?qued[óo]\s+(?:agendada|registrada|confirmada|tu\s+cita|tu\s+visita)|"
    r"ya\s+(?:est[áa]|qued[óo])\s+agendad[ao]|"
    r"agend[ée]\s+(?:tu|su|la)\s+(?:cita|visita)|"
    r"te\s+agendo\s+para|"
    r"tu\s+cita\s+(?:es|ser[áa]|qued[óo]|est[áa]\s+confirmada|qued[óo]\s+agendada)|"
    r"te\s+confirmo\s+(?:tu|la)\s+(?:cita|visita)|"
    r"lily\s+te\s+(?:confirma|comparte\s+la\s+direcci[óo]n)|"
    r"te\s+esperamos\s+el\s+\w+|"
    r"nos\s+vemos\s+el\s+(?:lunes|martes|mi[ée]rcoles|miercoles|jueves|viernes|s[áa]bado|sabado|domingo)",
    re.IGNORECASE,
)

# Marcadores condicionales/futuros que vuelven LEGÍTIMA la mención de registro
# ("cuando me confirmes los datos, registro tu solicitud") → NO se bloquea.
_CONDICIONAL_CITA_RE = re.compile(
    r"\b(cuando|una\s+vez|en\s+cuanto|apenas|si\s+me|para\s+(?:registrar|agendar|confirmar)|"
    r"necesito|me\s+confirm|me\s+compart|me\s+pas|antes\s+de|primero)\b",
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
        """Mapa para persistir en `sofia_turn_logs.validators_passed`. Solo errors
        (los warnings no se persisten en DB, ver ADR-018)."""
        return {r.validator: r.passed for r in self.results if r.severity == "error"}

    @property
    def failed_map(self) -> dict[str, str]:
        """Mapa para `sofia_turn_logs.validators_failed`. Solo errors."""
        return {
            r.validator: r.reason or "failed"
            for r in self.results
            if not r.passed and r.severity == "error"
        }

    @property
    def warnings_map(self) -> dict[str, str]:
        """Mapa de warnings (Bloque 5.7 ATAQUE 1). NO se persiste en DB —
        solo se loggea + se expone en TurnResult.validators_warnings."""
        return {
            r.validator: r.reason or "warning"
            for r in self.results
            if not r.passed and r.severity == "warning"
        }

    def feedback_para_regenerar(self) -> str | None:
        """Construye el texto que se inyecta al prompt para que el modelo regenere.
        SOLO considera errors — los warnings no disparan regeneración."""
        fails = [r for r in self.failed if r.severity == "error"]
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


def validar_no_markdown_excesivo(respuesta: str) -> ValidationResult:
    """Falla si la respuesta usa markdown que se ve mal en WhatsApp/Telegram.

    Reglas:
    - Headers (#, ##, ###) → siempre prohibidos en chat.
    - Más de 3 negritas `**...**` en una respuesta → estructura tipo documento.
    - Más de 4 bullets `- ` o `* ` consecutivos → lista densa, no conversacional.
    - Más de 3 ítems numerados `1. 2. 3.` → cuestionario, no conversación.

    Pasa si la respuesta es conversacional, máximo con 1-2 negritas o bullets
    cortos.
    """
    headers = _MARKDOWN_HEADER_RE.findall(respuesta)
    if headers:
        return ValidationResult(
            validator="no_markdown_excesivo",
            passed=False,
            reason=f"Usa headers (#) que en chat se ven raros: {len(headers)} encontrados",
            suggested_fix=(
                "Eliminá todos los headers tipo `#`, `##`, `###`. "
                "El chat es prosa natural, NO documento estructurado."
            ),
        )

    bolds = _MARKDOWN_BOLD_RE.findall(respuesta)
    if len(bolds) > 3:
        return ValidationResult(
            validator="no_markdown_excesivo",
            passed=False,
            reason=f"Demasiadas negritas: {len(bolds)} (máximo 3)",
            suggested_fix=(
                f"Tienes {len(bolds)} `**negritas**`. Reduce a máximo 2-3. "
                "El énfasis excesivo se ve a venta agresiva."
            ),
        )

    bullets = _MARKDOWN_BULLET_RE.findall(respuesta)
    if len(bullets) > 4:
        return ValidationResult(
            validator="no_markdown_excesivo",
            passed=False,
            reason=f"Lista densa con {len(bullets)} bullets (máximo 4)",
            suggested_fix=(
                f"Tienes {len(bullets)} bullets con `-` o `*`. Reescribe como prosa: "
                "1-2 oraciones conectadas. Bullets largos cansan al lector y se ven a manual."
            ),
        )

    numbered = _MARKDOWN_NUMBERED_RE.findall(respuesta)
    if len(numbered) > 3:
        return ValidationResult(
            validator="no_markdown_excesivo",
            passed=False,
            reason=f"Lista numerada con {len(numbered)} ítems (máximo 3)",
            suggested_fix=(
                "Las listas numeradas largas suenan a cuestionario. "
                "Usa prosa natural o reduce a 2-3 ítems."
            ),
        )

    return ValidationResult(validator="no_markdown_excesivo", passed=True)


def validar_no_inventa_datos(
    respuesta: str,
    estado: EstadoCapturado,
    mensajes_papa: list[str] | None = None,
) -> ValidationResult:
    """**Severity=warning** — registra señal sin disparar regeneración.

    Falla si la respuesta afirma datos que NO están en estado_capturado ni en
    los mensajes previos del papá. Ataca causa raíz #1 (5.6 — re-introducido
    en 5.7 ATAQUE 1 sin estricticidad).

    7 sub-chequeos: vio contenido externo / nombre papá / nivel del hijo /
    edad / género / campus / cita agendada. Conservador en saludo inicial
    (estado y mensajes_papa completamente vacíos → no falla por género).
    """
    mensajes_papa = mensajes_papa or []
    texto_papa = " ".join(mensajes_papa).lower()

    def _fail(reason: str, suggested_fix: str) -> ValidationResult:
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=reason,
            suggested_fix=suggested_fix,
            severity="warning",
        )

    # 1. Vio contenido externo (siempre falla — Sofía no tiene web)
    m = _AFIRMA_VIO_CONTENIDO_RE.search(respuesta)
    if m:
        return _fail(
            f"Afirma haber visto contenido externo: '{m.group(0)}'",
            "Sofía no tiene acceso web. Si el papá compartió un enlace, agradécelo y pregunta qué le llamó la atención sin pretender haberlo visto.",
        )

    # 2. Nombre del papá → movido a `validar_no_inventa_nombre_papa`
    #    (FIX 4, 2026-05-29 — ADR-020): este sub-check se separó y se subió a
    #    severity=error porque inventar el nombre del papá es de los peores fallos
    #    (rompe confianza al instante). El resto de sub-chequeos sigue en warning.

    # 3. Nivel del hijo afirmado sin respaldo
    niveles_conocidos: set[str] = set()
    if estado.nivel_buscado_actual:
        niveles_conocidos.add(estado.nivel_buscado_actual.value)
    for h in estado.hijos:
        if h.nivel:
            niveles_conocidos.add(h.nivel.value)
        if h.grado:
            niveles_conocidos.add(h.grado.lower())
    for m in _AFIRMA_NIVEL_HIJO_RE.finditer(respuesta):
        nivel_afirmado = m.group(1).lower().replace(" ", "")
        if any(
            n.replace(" ", "") in nivel_afirmado or nivel_afirmado in n for n in niveles_conocidos
        ):
            continue
        if any(token in texto_papa for token in [nivel_afirmado, nivel_afirmado[:5]]):
            continue
        return _fail(
            f"Afirma nivel '{m.group(1)}' sin respaldo",
            f"NO afirmes que el hijo está en {m.group(1)} si no aparece en estado_capturado ni en lo dicho.",
        )

    # 4. Edad del hijo afirmada sin respaldo
    edades_conocidas: set[int] = {h.edad for h in estado.hijos if h.edad is not None}
    for m in _AFIRMA_EDAD_HIJO_RE.finditer(respuesta):
        try:
            edad_afirmada = int(m.group(1))
        except ValueError:
            continue
        if edad_afirmada in edades_conocidas:
            continue
        if re.search(rf"\b{edad_afirmada}\s+(?:a[ñn]os?|meses)", texto_papa):
            continue
        return _fail(
            f"Afirma edad {edad_afirmada} sin respaldo",
            f"NO afirmes {edad_afirmada} años — pregunta si necesitas el dato.",
        )

    # 5. Género del hijo — tolerante en saludo vacío
    m_gen = _AFIRMA_GENERO_HIJO_RE.search(respuesta)
    if m_gen:
        genero_afirmado = m_gen.group(1).lower()
        papa_dio_referente = genero_afirmado in texto_papa or any(
            w in texto_papa for w in ("hijos", "hijas", "peque", "niño", "niña", "nino", "nina")
        )
        estado_tiene_referente = bool(estado.hijos) or estado.nivel_buscado_actual is not None
        contexto_vacio = not estado_tiene_referente and not texto_papa.strip()
        if not contexto_vacio and not papa_dio_referente and not estado_tiene_referente:
            return _fail(
                f"Afirma género '{genero_afirmado}' sin que el papá lo haya indicado",
                f"Usa 'tu peque' en lugar de 'tu {genero_afirmado}' si no sabes el género.",
            )

    # 6. Campus que contradice estado.campus_cita
    m_camp = _AFIRMA_CAMPUS_RE.search(respuesta)
    if m_camp:
        campus_afirmado = m_camp.group(1).lower().replace(" ", "")
        campus_estado = (estado.campus_cita or "").lower().replace(" ", "")
        if campus_estado and campus_estado not in campus_afirmado:
            return _fail(
                f"Afirma '{m_camp.group(1)}' pero estado tiene {estado.campus_cita}",
                f"El campus correcto es {estado.campus_cita}.",
            )

    # 7. Cita agendada falsa
    m_cit = _AFIRMA_CITA_AGENDADA_RE.search(respuesta)
    if m_cit and not estado.cita_agendada:
        return _fail(
            f"Afirma cita agendada ('{m_cit.group(0)}') pero estado.cita_agendada=False",
            "Propón la cita como invitación, no como hecho.",
        )

    return ValidationResult(validator="no_inventa_datos", passed=True, severity="warning")


def validar_sin_guiones_largos(respuesta: str) -> ValidationResult:
    """**Severity=error** — D.1 (Gaby 2026-05-27).

    Falla si la respuesta contiene em-dash (—) o en-dash (–). Esos caracteres
    son señal de texto de IA y rompen el registro de chat informal mexicano.
    Sofía debe usar punto, coma o dos puntos para conectar ideas.

    Pasa cualquier guión-minus normal (-) — los bullets en datos estructurados
    son aceptables (lista de costos, horarios). El validator de markdown
    excesivo ya limita su abuso.
    """
    match = _GUION_LARGO_RE.search(respuesta)
    if match is None:
        return ValidationResult(validator="sin_guiones_largos", passed=True)
    char = match.group(0)
    nombre = "guión largo (—)" if char == "—" else "guión medio (–)"
    return ValidationResult(
        validator="sin_guiones_largos",
        passed=False,
        reason=f"Usa {nombre} para conectar frases — suena a texto de IA",
        suggested_fix=(
            f"Quita TODOS los {nombre} de tu respuesta. Reemplázalos por punto, "
            "coma o dos puntos. Ejemplo: 'Maple no es escuela tradicional — es activa' "
            "→ 'Maple no es escuela tradicional. Es activa.'"
        ),
    )


def validar_no_recita_info_no_pedida(respuesta: str, intent: Intent | None) -> ValidationResult:
    """**Severity=warning** — Bloque 5.7 ATAQUE 2.

    Cuando el intent fue `RESPUESTA_CORTA_AL_TURNO_PREVIO`, la respuesta de
    Sofía NO debería ser una recitación larga de info no pedida. Falla
    (warning) si:
      - Respuesta > 80 palabras, O
      - Contiene headers (#) o numeración (1. 2. 3.) — señal de recital.

    Si el intent es otro, pasa sin chequear.
    """
    if intent != Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO:
        return ValidationResult(
            validator="no_recita_info_no_pedida", passed=True, severity="warning"
        )

    n_palabras = len(respuesta.split())
    headers = _MARKDOWN_HEADER_RE.findall(respuesta)
    numbered = _MARKDOWN_NUMBERED_RE.findall(respuesta)

    if n_palabras > 80:
        return ValidationResult(
            validator="no_recita_info_no_pedida",
            passed=False,
            reason=f"Respuesta de {n_palabras} palabras tras mensaje corto del papá",
            suggested_fix=(
                "El papá dio una respuesta corta. NO recites información no pedida. "
                "Responde con máximo 60 palabras y avanza el journey 1 paso."
            ),
            severity="warning",
        )
    if headers:
        return ValidationResult(
            validator="no_recita_info_no_pedida",
            passed=False,
            reason="Headers en respuesta a mensaje corto",
            suggested_fix="Quita headers — el papá pidió conversación, no folleto.",
            severity="warning",
        )
    if len(numbered) >= 2:
        return ValidationResult(
            validator="no_recita_info_no_pedida",
            passed=False,
            reason=f"Lista numerada de {len(numbered)} ítems tras mensaje corto",
            suggested_fix="Quita la lista numerada — responde en prosa breve.",
            severity="warning",
        )

    return ValidationResult(validator="no_recita_info_no_pedida", passed=True, severity="warning")


def validar_no_bullets_en_descubrimiento(
    respuesta: str, fase_journey: FaseJourney
) -> ValidationResult:
    """**Severity=warning** — señala bullets/listas excesivos en fase descubrimiento.

    Bloque 5.7 ATAQUE 1: criterio simple basado en fase del journey (NO en
    intimacy_detector complejo). En descubrimiento, los bullets/numerados/
    negritas excesivos son señal de tono transaccional. Thresholds calibrados
    del Bloque 5.6 PASO 5.0: ≥3 bullets, ≥3 numerados, o ≥4 negritas.
    """
    if fase_journey != FaseJourney.DESCUBRIMIENTO:
        return ValidationResult(
            validator="no_bullets_descubrimiento", passed=True, severity="warning"
        )

    bullets = _MARKDOWN_BULLET_RE.findall(respuesta)
    numbered = _MARKDOWN_NUMBERED_RE.findall(respuesta)
    bolds = _MARKDOWN_BOLD_RE.findall(respuesta)

    if len(bullets) >= 3 or len(numbered) >= 3 or len(bolds) >= 4:
        n = max(len(bullets), len(numbered), len(bolds))
        return ValidationResult(
            validator="no_bullets_descubrimiento",
            passed=False,
            reason=f"En descubrimiento con {n} bullets/numerados/negritas — tono transaccional",
            suggested_fix=(
                "En descubrimiento, prefiere prosa fluida. Bullets/listas solo cuando "
                "respondes preguntas operativas concretas (horarios, costos, requisitos)."
            ),
            severity="warning",
        )

    return ValidationResult(validator="no_bullets_descubrimiento", passed=True, severity="warning")


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
# FIX 4 (ADR-020) — Nombre del papá inventado, severity=error
# ============================================================


def validar_no_inventa_nombre_papa(
    respuesta: str,
    estado: EstadoCapturado,
    mensajes_papa: list[str] | None = None,
) -> ValidationResult:
    """**Severity=error** — bloquea si Sofía usa un nombre propio para el papá
    que NO está en `estado.nombre_papa` ni en lo que el papá escribió.

    Causa raíz del bug "María" (2026-05-29): el LLM alucinó un nombre. El
    sub-check existía en `validar_no_inventa_datos` pero como warning, así que
    detectaba sin bloquear. ADR-020: se separa y se sube SOLO este caso a error
    (no todo el bloque, que ADR-017 mantuvo en warning por sobre-regeneración).

    Conservador: requiere saludo vocativo + nombre capitalizado, y descarta
    palabras de una denylist. Si dudamos, NO bloqueamos.
    """
    mensajes_papa = mensajes_papa or []
    texto_papa = " ".join(mensajes_papa).lower()
    nombre_estado = (estado.nombre_papa or "").strip().lower()

    for m in _NOMBRE_VOCATIVO_RE.finditer(respuesta):
        candidato = m.group(1)
        cand_low = candidato.lower()
        if cand_low in _PALABRAS_NO_NOMBRE:
            continue
        if nombre_estado and cand_low in nombre_estado:
            continue
        if cand_low in texto_papa:
            continue
        return ValidationResult(
            validator="no_inventa_nombre_papa",
            passed=False,
            reason=f"Usa el nombre '{candidato}' que el papá nunca dio ni está en el estado",
            suggested_fix=(
                f"Borra el nombre '{candidato}'. El papá NUNCA te dijo su nombre. "
                f"Saluda sin nombre (ej. 'Hola, con gusto te ayudo'). NUNCA inventes "
                f"ni asumas el nombre del papá."
            ),
            severity="error",
        )

    return ValidationResult(validator="no_inventa_nombre_papa", passed=True, severity="error")


# ============================================================
# FIX 2/3 (ADR-021) — Confirmación de cita inexistente, severity=error
# ============================================================


def validar_no_confirma_cita_inexistente(
    respuesta: str,
    cita_realmente_registrada: bool,
) -> ValidationResult:
    """**Severity=error** — bloquea si Sofía AFIRMA haber registrado/confirmado
    una cita cuando NO existe un appointment_id real (`cita_realmente_registrada`
    es False).

    Ataca los bugs 2 y 3 (2026-05-29): sin los 6 datos el backend NO crea la
    cita, pero el LLM igual decía "registré tu solicitud, Lily te comparte la
    dirección". Este gate fuerza a regenerar pidiendo los datos faltantes en vez
    de confirmar algo que no pasó.

    Calibrado para NO bloquear mensajes de PROCESO/condicionales ("cuando me
    confirmes los datos, registro tu solicitud"): se evalúa oración por oración
    y se ignoran las que contienen un marcador condicional/futuro.
    """
    if cita_realmente_registrada:
        return ValidationResult(validator="no_confirma_cita_inexistente", passed=True)

    # Evaluar oración por oración: una confirmación declarativa SIN condicional.
    oraciones = re.split(r"[.!?\n]+", respuesta)
    for oracion in oraciones:
        if not oracion.strip():
            continue
        if _CONFIRMA_CITA_RE.search(oracion) and not _CONDICIONAL_CITA_RE.search(oracion):
            m = _CONFIRMA_CITA_RE.search(oracion)
            return ValidationResult(
                validator="no_confirma_cita_inexistente",
                passed=False,
                reason=(
                    f"Confirma/registra una cita ('{m.group(0).strip()}') pero NO existe "
                    f"una cita real (faltan datos o no se ha creado)"
                ),
                suggested_fix=(
                    "NO digas que registraste, agendaste ni confirmaste la cita: todavía "
                    "no existe. Pide de forma cálida los datos que faltan (nombre, correo, "
                    "celular, grado) o propón el día como invitación. NUNCA prometas que "
                    "'Lily te comparte la dirección' antes de registrar la cita."
                ),
                severity="error",
            )

    return ValidationResult(validator="no_confirma_cita_inexistente", passed=True)


# ============================================================
# Runner — ejecuta todos los validators
# ============================================================


def run_all_validators(
    respuesta: str,
    estado: EstadoCapturado,
    intent: Intent | None = None,
    tools_called: list[str] | None = None,
    frases_usadas: list[str] | None = None,
    mensajes_papa: list[str] | None = None,
    fase_journey: FaseJourney | None = None,
    cita_realmente_registrada: bool = False,
) -> ValidationReport:
    """Ejecuta todos los validators secuencialmente y agrega resultados.

    Es pura: no escribe DB, no llama APIs. Solo razona sobre el texto.

    Bloque 5.7 ATAQUE 1: los validators heurísticos nuevos
    (`no_inventa_datos`, `no_bullets_descubrimiento`) devuelven
    `severity="warning"` — NO disparan regeneración, solo se loggean y se
    exponen vía `report.warnings_map`.

    `mensajes_papa`: lista de mensajes previos del papá, usada por
    `no_inventa_datos` para corroborar entidades.

    `fase_journey`: usada por `no_bullets_descubrimiento` para activar
    solo en fase descubrimiento.
    """
    report = ValidationReport()
    report.results.append(validar_no_repeticion(respuesta, frases_usadas or []))
    report.results.append(validar_no_envio_fantasma(respuesta, tools_called))
    report.results.append(validar_no_pregunta_repetida(respuesta, estado))
    report.results.append(validar_no_evasion(respuesta, intent))
    report.results.append(validar_no_markdown_excesivo(respuesta))
    report.results.append(validar_sin_guiones_largos(respuesta))
    # FIX 4 (ADR-020) + FIX 2/3 (ADR-021) — severity=error (sí bloquean/regeneran)
    report.results.append(validar_no_inventa_nombre_papa(respuesta, estado, mensajes_papa))
    report.results.append(
        validar_no_confirma_cita_inexistente(respuesta, cita_realmente_registrada)
    )
    # Warnings heurísticos del 5.7 — no bloquean
    report.results.append(validar_no_inventa_datos(respuesta, estado, mensajes_papa))
    if fase_journey is not None:
        report.results.append(validar_no_bullets_en_descubrimiento(respuesta, fase_journey))
    # ATAQUE 2: validator de soporte para RESPUESTA_CORTA_AL_TURNO_PREVIO
    report.results.append(validar_no_recita_info_no_pedida(respuesta, intent))
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
