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

# Bloque 5.6 — patrones para validar_no_inventa_datos

# Afirmaciones de haber accedido a contenido externo (Sofía no tiene visión web)
_AFIRMA_VIO_CONTENIDO_RE = re.compile(
    r"\bvi\s+(?:el|tu|la|los?|las?)\s+(?:link|enlace|imagen|video|contenido|post|publicaci[oó]n|art[ií]culo|p[aá]gina)\b|"
    r"\b(?:revis[eé]|le[íi]|mir[eé])\s+(?:el|tu|la|los?)\s+(?:link|enlace|contenido|post)\b|"
    r"\b(?:le[íi]|vi)\s+lo\s+que\s+(?:dice|me\s+enviaste|compart[ií]ste|compart[ií]aste)\b|"
    r"acabo\s+de\s+ver\s+(?:el|tu)",
    re.IGNORECASE,
)

# Afirmar nombre del papá ("Hola Juan, ...", "Mira Juan, ...").
# El grupo capturado es case-SENSITIVE (debe empezar con mayúscula) para no
# matchear muletillas comunes ("qué", "cuánto", "claro", etc.) que vienen después
# de saludos. Las saludo-keywords sí toleran case mixto.
_AFIRMA_NOMBRE_PAPA_RE = re.compile(
    r"(?:^|\.\s+|,\s+)(?:[Hh]ola|[Mm]ira|[Ff]íjate|[Oo]ye|[Cc]laro|[Ss][ií])[,]?\s+"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,15})\b"
)

# Afirmar nivel/etapa del hijo (afirmativo, no interrogativo)
# Ejemplos que matchean: "tu hijo de Kinder", "para Maternal", "tu peque está en primaria"
# NO matchea: "¿qué nivel busca?", "¿para Kinder o Maternal?"
_AFIRMA_NIVEL_HIJO_RE = re.compile(
    r"\btu\s+(?:hijo|hija|peque[ñn]o|peque[ñn]a|peque|ni[ñn]o|ni[ñn]a)\s+"
    r"(?:de|en|est[aá]\s+en|va\s+a|busca)\s+(maternal|kinder|preescolar|primaria|secundaria|"
    r"\d+\s*°\s*(?:de\s+)?(?:primaria|secundaria|kinder)|"
    r"infants|toddlers|cubs|baby|preschool)\b",
    re.IGNORECASE,
)

# Afirmar edad ("tu hijo de N años") — solo afirmación, no pregunta
_AFIRMA_EDAD_HIJO_RE = re.compile(
    r"\btu\s+(?:hijo|hija|peque|ni[ñn]o|ni[ñn]a)\s+de\s+(\d{1,2})\s+(?:a[ñn]os?|meses)\b",
    re.IGNORECASE,
)

# Afirmar género del hijo cuando solo se ha mencionado neutralmente
_AFIRMA_GENERO_HIJO_RE = re.compile(
    r"\btu\s+(hijo|hija)\b(?!\s*[oó]\s*(?:hija|hijo))",
    re.IGNORECASE,
)

# Afirmar campus específico ("en Campus 2", "vamos a Campus 1")
_AFIRMA_CAMPUS_RE = re.compile(
    r"\b(?:en\s+|para\s+|al\s+|del?\s+|tu\s+(?:cita|visita)\s+(?:es\s+)?en\s+)"
    r"(campus\s*[12])\b",
    re.IGNORECASE,
)

# Afirmar cita ya agendada ("ya agendaste", "tu cita es el", "te espero el")
_AFIRMA_CITA_AGENDADA_RE = re.compile(
    r"\b(?:ya\s+agendaste|tu\s+cita\s+(?:es|ser[aá]|qued[oó]|est[aá]\s+confirmada)|"
    r"tu\s+visita\s+(?:es|ser[aá]|qued[oó])|"
    r"te\s+espero\s+el\s+\w+|nos\s+vemos\s+el\s+\w+)",
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
    """Falla si la respuesta afirma datos que NO están en estado_capturado ni en
    los mensajes previos del papá. Ataca la causa raíz #1 del Bloque 5.6.

    Detecta los siguientes patrones de invención:
    1. Afirmar haber visto contenido externo (URL, imagen, post) — Sofía no tiene
       acceso web; siempre falla.
    2. Afirmar nombre del papá si no está en `estado.nombre_papa` ni en mensajes
       previos del papá.
    3. Afirmar nivel del hijo si no coincide con `estado.nivel_buscado_actual`
       ni con ningún `estado.hijos[].nivel`.
    4. Afirmar edad del hijo si no coincide con `estado.hijos[].edad`.
    5. Afirmar género (hijo vs hija) cuando no aparece en estado ni en mensajes
       previos del papá.
    6. Afirmar Campus específico si no coincide con `estado.campus_cita`.
    7. Afirmar cita agendada si `estado.cita_agendada` es False.

    Es conservador: solo falla cuando hay evidencia clara de invención.
    Preguntas hipotéticas ("¿para Maternal?") NO fallan.

    `mensajes_papa`: lista de strings con los mensajes previos del papá (sin
    respuestas de Sofía). Útil para corroborar datos que el extractor podría
    no haber capturado aún.
    """
    mensajes_papa = mensajes_papa or []
    texto_papa = " ".join(mensajes_papa).lower()

    # 1. Afirmar haber visto contenido externo — sin tool de visión web, siempre falla
    m = _AFIRMA_VIO_CONTENIDO_RE.search(respuesta)
    if m:
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=f"Afirma haber visto contenido externo: '{m.group(0)}'. Sofía no tiene acceso web.",
            suggested_fix=(
                "NUNCA afirmes haber visto links, imágenes, posts, videos ni contenido externo. "
                "No tienes acceso web. Si el papá compartió un enlace, agradécelo y pregunta "
                "qué le llamó la atención de eso, sin pretender haberlo visto."
            ),
        )

    # 2. Afirmar nombre del papá
    nombre_estado = (estado.nombre_papa or "").strip().lower()
    nombres_que_no_son_papa = {"sof", "sofía", "sofia"}  # autoreferencia OK
    for m in _AFIRMA_NOMBRE_PAPA_RE.finditer(respuesta):
        candidato = m.group(1).lower()
        if candidato in nombres_que_no_son_papa:
            continue
        if nombre_estado and candidato in nombre_estado:
            continue
        # Verificar si el nombre aparece en algún mensaje previo del papá
        if candidato in texto_papa:
            continue
        # Posible invención
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=f"Usa nombre '{m.group(1)}' que no está en estado ni en mensajes del papá",
            suggested_fix=(
                f"Borra el nombre '{m.group(1)}' — el papá no te lo ha dado. Si quieres "
                "personalizar, simplemente saluda sin nombre."
            ),
        )

    # 3. Afirmar nivel del hijo
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
        # Normalización para coincidencias parciales (maternal ≈ early years, etc.)
        if any(
            n.replace(" ", "") in nivel_afirmado or nivel_afirmado in n for n in niveles_conocidos
        ):
            continue
        # Buscar en mensajes del papá (literal o variante)
        if any(token in texto_papa for token in [nivel_afirmado, nivel_afirmado[:5]]):
            continue
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=f"Afirma nivel '{m.group(1)}' del hijo sin que el papá lo haya mencionado",
            suggested_fix=(
                f"NO afirmes que el hijo está en {m.group(1)} — no aparece en lo que el papá te ha dicho "
                "ni en estado_capturado. Si necesitas saberlo, pregúntalo abiertamente."
            ),
        )

    # 4. Afirmar edad del hijo
    edades_conocidas: set[int] = {h.edad for h in estado.hijos if h.edad is not None}
    for m in _AFIRMA_EDAD_HIJO_RE.finditer(respuesta):
        try:
            edad_afirmada = int(m.group(1))
        except ValueError:
            continue
        if edad_afirmada in edades_conocidas:
            continue
        # Buscar literal en mensajes del papá
        if re.search(rf"\b{edad_afirmada}\s+(?:a[ñn]os?|meses)", texto_papa):
            continue
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=f"Afirma edad {edad_afirmada} sin que el papá la haya mencionado",
            suggested_fix=(
                f"NO afirmes que el hijo tiene {edad_afirmada} años — no está en estado_capturado "
                "ni en lo que el papá te ha dicho. Pregúntale la edad si la necesitas."
            ),
        )

    # 5. Género del hijo (hijo/hija) — falla SOLO cuando:
    #    - No hay info ni de hijos ni de nivel buscado en estado (no hay contexto)
    #    - El papá NO mencionó "hijo"/"hija"/"niño"/"niña"/"peque" en mensajes
    #    - El papá NO compartió nivel ("maternal", "kinder", etc.)
    # Si hay nivel_buscado_actual o estado.hijos, tolerar "tu hijo/hija" — el
    # contexto justifica que ya estamos discutiendo a alguien.
    m_gen = _AFIRMA_GENERO_HIJO_RE.search(respuesta)
    if m_gen:
        genero_afirmado = m_gen.group(1).lower()
        papa_dio_referente = genero_afirmado in texto_papa or any(
            w in texto_papa for w in ("hijos", "hijas", "peque", "niño", "niña", "nino", "nina")
        )
        estado_tiene_referente = bool(estado.hijos) or estado.nivel_buscado_actual is not None
        if not papa_dio_referente and not estado_tiene_referente:
            return ValidationResult(
                validator="no_inventa_datos",
                passed=False,
                reason=f"Afirma género '{genero_afirmado}' sin que el papá lo haya indicado",
                suggested_fix=(
                    f"NO digas 'tu {genero_afirmado}' — no sabes el género. Usa 'tu peque' o "
                    "pregunta abiertamente sin asumir."
                ),
            )

    # 6. Campus específico
    m_camp = _AFIRMA_CAMPUS_RE.search(respuesta)
    if m_camp:
        campus_afirmado = m_camp.group(1).lower().replace(" ", "")  # "campus1" o "campus2"
        campus_estado = (estado.campus_cita or "").lower().replace(" ", "")
        if campus_estado and campus_estado not in campus_afirmado:
            return ValidationResult(
                validator="no_inventa_datos",
                passed=False,
                reason=f"Afirma '{m_camp.group(1)}' pero estado tiene {estado.campus_cita}",
                suggested_fix=(
                    f"El campus correcto es {estado.campus_cita} (según estado_capturado). "
                    f"Reescribe sin contradecirlo."
                ),
            )

    # 7. Cita agendada falsa
    m_cit = _AFIRMA_CITA_AGENDADA_RE.search(respuesta)
    if m_cit and not estado.cita_agendada:
        return ValidationResult(
            validator="no_inventa_datos",
            passed=False,
            reason=f"Afirma cita agendada ('{m_cit.group(0)}') pero estado.cita_agendada=False",
            suggested_fix=(
                "NO afirmes que la cita está agendada — no lo está. Si quieres proponerla, "
                "hazlo como invitación ('¿te gustaría agendar?'), no como hecho."
            ),
        )

    return ValidationResult(validator="no_inventa_datos", passed=True)


def validar_no_bullets_en_momento_intimo(
    respuesta: str, es_momento_intimo: bool
) -> ValidationResult:
    """Si el momento es íntimo y la respuesta usa bullets/listas/numeración, falla.

    Más estricto que `no_markdown_excesivo`: aquí basta CON 2+ bullets o 2+ items
    numerados (no requerimos densidad alta). Ataca la causa raíz #2: tono
    transaccional con bullets en momentos íntimos.
    """
    if not es_momento_intimo:
        return ValidationResult(validator="no_bullets_intimo", passed=True)

    bullets = _MARKDOWN_BULLET_RE.findall(respuesta)
    numbered = _MARKDOWN_NUMBERED_RE.findall(respuesta)
    bolds = _MARKDOWN_BOLD_RE.findall(respuesta)

    if len(bullets) >= 2 or len(numbered) >= 2 or len(bolds) >= 3:
        n_items = max(len(bullets), len(numbered), len(bolds))
        return ValidationResult(
            validator="no_bullets_intimo",
            passed=False,
            reason=f"Momento íntimo con estructura visual: {n_items} bullets/numerados/negritas",
            suggested_fix=(
                "Este es un momento íntimo de la conversación (el papá está hablando "
                "emocionalmente o pidiendo continuar tras un momento personal). "
                "Reescribe TODO en prosa fluida, 2-4 oraciones máximo, sin bullets, "
                "sin listas numeradas, sin negritas excesivas. Habla con calidez "
                "humana, no con estructura comercial."
            ),
        )

    return ValidationResult(validator="no_bullets_intimo", passed=True)


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
    mensajes_papa: list[str] | None = None,
    es_momento_intimo: bool = False,
) -> ValidationReport:
    """Ejecuta todos los validators secuencialmente y agrega resultados.

    Es pura: no escribe DB, no llama APIs. Solo razona sobre el texto.

    `mensajes_papa` (Bloque 5.6 PASO 1) es la lista de mensajes anteriores del
    papá (sin las respuestas de Sofía), usada por `validar_no_inventa_datos`.

    `es_momento_intimo` (Bloque 5.6 PASO 3) viene del detector de intimidad —
    cuando True, el validator `no_bullets_intimo` exige prosa.
    """
    report = ValidationReport()
    report.results.append(validar_no_repeticion(respuesta, frases_usadas or []))
    report.results.append(validar_no_envio_fantasma(respuesta, tools_called))
    report.results.append(validar_no_pregunta_repetida(respuesta, estado))
    report.results.append(validar_no_evasion(respuesta, intent))
    report.results.append(validar_no_markdown_excesivo(respuesta))
    report.results.append(validar_no_inventa_datos(respuesta, estado, mensajes_papa))
    report.results.append(validar_no_bullets_en_momento_intimo(respuesta, es_momento_intimo))
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
