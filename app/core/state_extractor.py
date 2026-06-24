"""Extractor de estado: actualiza EstadoCapturado a partir del mensaje del usuario.

Usa gpt-4o-mini con structured output. Mantiene los datos del papá (nivel, edad,
escuela, miedos, etc.) actualizados turno a turno para inyectarlos al prompt y
evitar repreguntar.

Estrategia: en cada turno se envía al modelo (a) el estado actual capturado y
(b) el último mensaje, y se le pide que devuelva los campos NUEVOS detectados.
Hacemos merge defensivo (no sobreescribir si el modelo no detectó nada).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from app.adapters.openai_client import get_openai
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

log = logging.getLogger(__name__)


# ============================================================
# Extractor determinístico de GRADO (FIX 2026-06-01)
# ============================================================
#
# El extractor LLM a veces NO captura el grado de frases cortas como "2 kinder"
# (caso real de la prueba de Oscar: dejó grado=None y la cita no cerró). Este
# fallback lo normaliza por código: "2 kinder" → "2° de Kinder".

_NUM_PALABRA: dict[str, int] = {
    "1": 1,
    "1ro": 1,
    "1er": 1,
    "1°": 1,
    "primero": 1,
    "primer": 1,
    "primera": 1,
    "2": 2,
    "2do": 2,
    "2°": 2,
    "segundo": 2,
    "segunda": 2,
    "3": 3,
    "3ro": 3,
    "3er": 3,
    "3°": 3,
    "tercero": 3,
    "tercer": 3,
    "tercera": 3,
    "4": 4,
    "4to": 4,
    "4°": 4,
    "cuarto": 4,
    "cuarta": 4,
    "5": 5,
    "5to": 5,
    "5°": 5,
    "quinto": 5,
    "quinta": 5,
    "6": 6,
    "6to": 6,
    "6°": 6,
    "sexto": 6,
    "sexta": 6,
}
_NIVEL_DISPLAY: dict[str, str] = {
    "kinder": "Kinder",
    "kínder": "Kinder",
    "preescolar": "Kinder",
    "primaria": "Primaria",
    "secundaria": "Secundaria",
}
_NIVEL_ENUM: dict[str, str] = {
    "kinder": "kinder",
    "kínder": "kinder",
    "preescolar": "kinder",
    "primaria": "primaria",
    "secundaria": "secundaria",
    "maternal": "maternal",
}
# Multi-char primero para que "2do"/"2°" ganen al dígito suelto.
_NUMS_KW = (
    r"1ro|1er|1°|2do|2°|3ro|3er|3°|4to|4°|5to|5°|6to|6°|"
    r"primero|primera|primer|segundo|segunda|tercero|tercera|tercer|"
    r"cuarto|cuarta|quinto|quinta|sexto|sexta|[1-6]"
)
_NIVELES_KW = r"kinder|kínder|preescolar|primaria|secundaria"
_GRADO_NUM_NIVEL_RE = re.compile(
    rf"\b({_NUMS_KW})\s*(?:°\s*)?(?:de\s+|grado\s+(?:de\s+)?)?({_NIVELES_KW})\b", re.IGNORECASE
)
_GRADO_NIVEL_NUM_RE = re.compile(rf"\b({_NIVELES_KW})\s+({_NUMS_KW})\b", re.IGNORECASE)


def extraer_grado_simple(mensaje: str) -> tuple[str | None, str | None]:
    """('2 kinder') → ('2° de Kinder', 'kinder'). Devuelve (grado, nivel) o (None, None)."""
    m = (mensaje or "").lower()
    for rx, num_first in ((_GRADO_NUM_NIVEL_RE, True), (_GRADO_NIVEL_NUM_RE, False)):
        mt = rx.search(m)
        if not mt:
            continue
        num_t = mt.group(1) if num_first else mt.group(2)
        niv_t = mt.group(2) if num_first else mt.group(1)
        num = _NUM_PALABRA.get(num_t.strip())
        if num and niv_t in _NIVEL_DISPLAY:
            return f"{num}° de {_NIVEL_DISPLAY[niv_t]}", _NIVEL_ENUM.get(niv_t)
    return None, None


# ============================================================
# Nombre del NIÑO pegado a la edad (FIX (c) 2026-06-01)
# ============================================================
#
# "Jose, 4 años" → Jose es el NIÑO (nombre adyacente a la edad). El extractor LLM
# lo metía como nombre del papá (caso real: nombre_papa="Jose", y luego "Oscar
# Rodriguez" ya no podía sobreescribir). Este detector lo asigna al hijo.

_NOMBRE_EDAD_RE = re.compile(
    r"\b([a-záéíóúñ]{2,16})\b\s*,?\s+(?:de\s+|tiene\s+)?(\d{1,2})\s*a[ñn](?:o|os|ito|itos)?\b",
    re.IGNORECASE,
)
_NO_NOMBRE_EDAD = frozenset(
    {
        "tengo",
        "tiene",
        "tienen",
        "mi",
        "su",
        "hijo",
        "hija",
        "niño",
        "niña",
        "nino",
        "nina",
        "nene",
        "nena",
        "peque",
        "el",
        "la",
        "los",
        "las",
        "de",
        "del",
        "años",
        "año",
        "ya",
        "mas",
        "más",
        "casi",
        "como",
        "unos",
        "una",
        "uno",
        "y",
        "es",
        "son",
        "soy",
        "con",
        "para",
        "cumple",
        "cumplio",
        "cumplió",
    }
)


def _nombre_junto_a_edad(mensaje: str) -> str | None:
    """'Jose, 4 años' / 'Jose de 4 años' → 'Jose'. None si no hay nombre+edad."""
    for m in _NOMBRE_EDAD_RE.finditer(mensaje or ""):
        nombre = m.group(1)
        if nombre.lower() in _NO_NOMBRE_EDAD:
            continue
        return nombre[:1].upper() + nombre[1:].lower()
    return None


# ============================================================
# Capa de captura DETERMINÍSTICA consolidada (FIX 2026-06-02)
# ============================================================
#
# Principio del proyecto: el LLM NO es load-bearing para datos estructurados.
# Estos extractores corren SIEMPRE; los de alta confianza (email, teléfono,
# "yo soy X") MANDAN sobre el LLM; los demás rellenan lo que el LLM dejó vacío.

# Palabras que NO son nombre propio (de hijo ni de papá). Si el LLM devuelve una
# de éstas como nombre, se descarta → el gate la pedirá (NO se inventa).
_NO_ES_NOMBRE = _NO_NOMBRE_EDAD | frozenset(
    {
        "pequeño",
        "pequeña",
        "pequeno",
        "pequena",
        "peque",
        "pequeñito",
        "pequenito",
        "bebe",
        "bebé",
        "bb",
        "chiquito",
        "chiquita",
        "hijo",
        "hija",
        "niño",
        "niña",
        "nino",
        "nina",
        "nene",
        "nena",
        "hermano",
        "hermana",
        "mamá",
        "papá",
        "mama",
        "papa",
        "señor",
        "señora",
        "senor",
        "senora",
    }
)


def _es_nombre_valido(nombre: str | None) -> bool:
    """True si `nombre` parece un nombre propio real (no 'pequeño', 'tiene', etc.)."""
    if not nombre:
        return False
    n = nombre.strip().lower()
    if not n or n in _NO_ES_NOMBRE:
        return False
    # Debe empezar por letra y no ser solo símbolos/números.
    return bool(re.match(r"^[a-záéíóúñ]", n))


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Teléfono: 10-15 dígitos, admite +, espacios, guiones y paréntesis entre medias.
_TEL_RE = re.compile(r"\+?\d[\d\s\-().]{8,}\d")
# Edad: requiere la palabra años/añitos (evita 'tengo 4 hijos' → edad 4).
_EDAD_RE = re.compile(r"\b(\d{1,2})\s*a[ñn](?:o|os|ito|itos)?\b", re.IGNORECASE)
# Nombre del papá por presentación explícita ("yo soy X", "me llamo X", "soy X").
_NOMBRE_PAPA_CAP_RE = re.compile(
    r"(?:^|[\s,.;])(?:yo\s+soy|me\s+llamo|mi\s+nombre\s+es|soy)\s+"
    r"([a-záéíóúñ]+(?:\s+[a-záéíóúñ]+){0,3})",
    re.IGNORECASE,
)
# Tras "soy/me llamo", estas palabras cortan el nombre (no forman parte de él).
_STOP_NOMBRE_PAPA = frozenset(
    {
        "la",
        "el",
        "los",
        "las",
        "mama",
        "mamá",
        "papa",
        "papá",
        "de",
        "del",
        "y",
        "con",
        "para",
        "que",
        "mi",
        "su",
        "busco",
        "buscando",
        "interesad",
        "interesada",
        "interesado",
        "aqui",
        "aquí",
        "un",
        "una",
        "porque",
        "pero",
    }
)


def extraer_email(mensaje: str) -> str | None:
    """Primer email válido del mensaje, o None. Captura literal (sin tocar mayúsc.)."""
    m = _EMAIL_RE.search(mensaje or "")
    return m.group(0) if m else None


def extraer_telefono(mensaje: str, *, excluir: str | None = None) -> str | None:
    """Primer teléfono (10-15 dígitos) normalizado a '+?dígitos', o None.

    `excluir`: substring a remover antes de buscar (p.ej. el email ya extraído,
    para que sus dígitos no se confundan con un teléfono).
    """
    texto = mensaje or ""
    if excluir:
        texto = texto.replace(excluir, " ")
    for m in _TEL_RE.finditer(texto):
        raw = m.group(0)
        signo = "+" if raw.lstrip().startswith("+") else ""
        digitos = re.sub(r"\D", "", raw)
        if 10 <= len(digitos) <= 15:
            return signo + digitos
    return None


def extraer_edad_simple(mensaje: str) -> int | None:
    """'tiene 4 años' / '4 añitos' → 4. None si no hay edad con la palabra 'años'."""
    m = _EDAD_RE.search(mensaje or "")
    if not m:
        return None
    edad = int(m.group(1))
    return edad if 0 <= edad <= 20 else None


_EDAD_NUM_SUELTO_RE = re.compile(r"\b(\d{1,2})\b")


def extraer_edad_de_numero_suelto(mensaje: str) -> int | None:
    """'5' / 'tiene 5' → 5. Primer número 0-20 del mensaje. Usar SOLO cuando el
    gate pidió la edad (contexto), para no confundir un número con otra cosa."""
    m = _EDAD_NUM_SUELTO_RE.search(mensaje or "")
    if not m:
        return None
    edad = int(m.group(1))
    return edad if 0 <= edad <= 20 else None


def extraer_nombre_papa(mensaje: str) -> str | None:
    """'yo soy Pedro Rojas, ...' → 'Pedro Rojas'. None si no hay presentación
    explícita o el nombre no es válido."""
    m = _NOMBRE_PAPA_CAP_RE.search(mensaje or "")
    if not m:
        return None
    tokens: list[str] = []
    for t in m.group(1).split():
        tl = t.lower().strip(",.;")
        if tl in _STOP_NOMBRE_PAPA or not re.match(r"^[a-záéíóúñ]+$", tl):
            break
        tokens.append(tl)
        if len(tokens) >= 3:
            break
    if not tokens:
        return None
    nombre = " ".join(w[:1].upper() + w[1:] for w in tokens)
    return nombre if _es_nombre_valido(nombre) else None


# Nombre del HIJO presentado explícitamente, SIN edad adyacente: "se llama X",
# "mi hijo X", "mi hija se llama X", "el niño X". Cubre el gap del bucle real
# (el papá dijo "se llama Emanuel" y el LLM lo metía como nombre del papá).
_NOMBRE_HIJO_MARCADOR_RE = re.compile(
    r"\b(?:"
    r"se\s+llama|"
    r"mi\s+(?:hijo|hija|peque\w*|niñ[oa]|nin[oa]|beb[eé])\s+(?:se\s+llama\s+|es\s+)?|"
    r"el\s+niñ[oa]\s+(?:se\s+llama\s+)?|la\s+niñ[oa]\s+(?:se\s+llama\s+)?"
    r")\s*",
    re.IGNORECASE,
)


def extraer_nombre_hijo(mensaje: str) -> str | None:
    """'se llama Emanuel Rodriguez' → 'Emanuel Rodriguez'; 'mi hijo Emanuel' →
    'Emanuel'. Captura nombre + apellido (hasta 2 tokens), parando en palabras
    función. None si no hay nombre válido (descarta 'tiene', 'pequeño', etc.)."""
    m = _NOMBRE_HIJO_MARCADOR_RE.search(mensaje or "")
    if not m:
        return None
    tokens: list[str] = []
    for raw in (mensaje or "")[m.end() :].split():
        t = raw.lower().strip(",.;¿?¡!()")
        if not t:
            continue
        if (
            not re.match(r"^[a-záéíóúñ]+$", t)
            or t in _PALABRAS_NO_NOMBRE
            or not _es_nombre_valido(t)
        ):
            break
        tokens.append(t)
        if len(tokens) >= 2:  # nombre + apellido
            break
    if not tokens:
        return None
    return " ".join(w[:1].upper() + w[1:] for w in tokens)


# El papá/hijo responde un nombre SUELTO ("Oscar Rodriguez" / "Emanuel Rodriguez")
# cuando Sofía lo pidió. Sin marcador ("yo soy" / "se llama"), el LLM a veces no
# lo captura o lo mal-asigna → el gate seguía pidiéndolo en bucle / ghost-close.
# Captura por CONTEXTO según QUÉ pidió Sofía en su último turno.
_PIDE_NOMBRE_PAPA_RE = re.compile(
    r"tu\s+nombre|c[óo]mo\s+te\s+llamas|qui[eé]n\s+tengo\s+el\s+gusto|"
    r"me\s+(?:das|compartes|dices|regalas)\s+tu\s+nombre",
    re.IGNORECASE,
)
# Sofía pidió el nombre del HIJO ("nombre completo de tu hijo", "cómo se llama").
_PIDE_NOMBRE_HIJO_RE = re.compile(
    r"nombre\s+(?:completo\s+)?(?:de\s+tu|del?)\s+"
    r"(?:hijo|hija|peque\w*|niñ[oa]|nin[oa]|beb[eé]|alumn[oa])|"
    r"c[óo]mo\s+se\s+llama|nombre\s+del?\s+(?:niñ[oa]|peque\w*)",
    re.IGNORECASE,
)
# Palabras función / deflexiones que NO son nombre (descartan "si ya te lo dije",
# "ok claro", "cuánto cuesta", "no sé", etc.).
_PALABRAS_NO_NOMBRE = frozenset(
    {
        "si",
        "sí",
        "no",
        "ya",
        "te",
        "lo",
        "la",
        "le",
        "les",
        "me",
        "mi",
        "tu",
        "su",
        "eso",
        "esa",
        "ese",
        "esta",
        "este",
        "esto",
        "aqui",
        "aquí",
        "alli",
        "allí",
        "igual",
        "mismo",
        "dije",
        "dijo",
        "dado",
        "gracias",
        "ok",
        "okay",
        "dale",
        "claro",
        "va",
        "sale",
        # verbos comunes tras "mi hijo …" (no son nombres): "mi hijo VIENE de otra
        # escuela" daba el nombre "Viene". Bloqueamos los conjugados más frecuentes.
        "viene",
        "tiene",
        "está",
        "esta",
        "estaba",
        "anda",
        "entra",
        "entró",
        "cursa",
        "estudia",
        "estudió",
        "necesita",
        "quiere",
        "fue",
        "iba",
        "asiste",
        "vive",
        "viven",
        "pasa",
        "paso",
        "pasó",
        "bien",
        "asi",
        "así",
        "pues",
        "porque",
        "que",
        "cual",
        "cuando",
        "como",
        "cómo",
        "y",
        "o",
        "el",
        "ella",
        "ellos",
        "es",
        "son",
        "soy",
        # deflexiones / preguntas frecuentes
        "cuanto",
        "cuánto",
        "cuesta",
        "cuestan",
        "precio",
        "precios",
        "costo",
        "costos",
        "colegiatura",
        "mensualidad",
        "donde",
        "dónde",
        "quiero",
        "quisiera",
        "puedo",
        "podemos",
        "necesito",
        "hola",
        "buenas",
        "buenos",
        "dias",
        "días",
        "tardes",
        "informacion",
        "información",
        "info",
        "ayuda",
        "nose",
        "sé",
        "perdon",
        "perdón",
        "disculpa",
        "espera",
        "espere",
        "nada",
        "todavia",
        "todavía",
        "aun",
        "aún",
    }
)
# Prefijos de cortesía a ignorar antes del nombre ("soy Oscar", "me llamo Ana").
_PREFIJO_CORTESIA = frozenset({"soy", "me", "llamo", "mi", "nombre", "es", "yo"})


def _nombre_limpio_de_respuesta(mensaje: str) -> str | None:
    """Extrae un nombre LIMPIO de una respuesta que puede traer correo/teléfono
    junto ("Oscar Rodriguez, oscar@x.com, +52..." → "Oscar Rodriguez"). None si lo
    que queda no es un nombre (pregunta, palabras-función, vacío, >4 tokens)."""
    if "?" in (mensaje or "") or "¿" in (mensaje or ""):  # es una pregunta, no un nombre
        return None
    txt = _EMAIL_RE.sub(" ", mensaje or "")
    txt = _TEL_RE.sub(" ", txt)
    tokens: list[str] = []
    for raw in re.split(r"[\s,;]+", txt):
        t = raw.lower().strip(",.;¿?¡!()")
        if not t:
            continue
        if t in _PREFIJO_CORTESIA:  # ignora "soy", "me llamo"…
            continue
        if t in _PALABRAS_NO_NOMBRE or not re.match(r"^[a-záéíóúñ]+$", t):
            return None  # token que no es nombre → el mensaje no es un nombre limpio
        tokens.append(t)
    if not tokens or len(tokens) > 4:
        return None
    nombre = " ".join(w[:1].upper() + w[1:] for w in tokens)
    return nombre if _es_nombre_valido(nombre) else None


def nombre_papa_por_contexto(mensaje: str, ultimo_assistant: str | None) -> str | None:
    """'Oscar Rodriguez' como respuesta a "¿tu nombre?" → 'Oscar Rodriguez'.

    Solo dispara si el ÚLTIMO turno de Sofía pidió el nombre del PAPÁ y el mensaje
    es un nombre limpio (admite correo/teléfono junto)."""
    if not ultimo_assistant or not _PIDE_NOMBRE_PAPA_RE.search(ultimo_assistant):
        return None
    txt = (mensaje or "").strip()
    # Si es una presentación del HIJO ("se llama X", "Ana, 4 años"), NO es el papá.
    if extraer_nombre_hijo(txt) or _nombre_junto_a_edad(txt):
        return None
    return _nombre_limpio_de_respuesta(txt)


def nombre_hijo_por_contexto(mensaje: str, ultimo_assistant: str | None) -> str | None:
    """'Emanuel Rodriguez' como respuesta a "¿nombre completo de tu hijo?" →
    'Emanuel Rodriguez'. Simétrico de nombre_papa_por_contexto: un nombre SUELTO
    del hijo (sin "se llama") debe capturarse igual."""
    if not ultimo_assistant or not _PIDE_NOMBRE_HIJO_RE.search(ultimo_assistant):
        return None
    return _nombre_limpio_de_respuesta((mensaje or "").strip())


def _tomar_nombre_inicial(texto: str, *, max_tokens: int = 3) -> str | None:
    """Toma los primeros tokens-nombre de `texto` (parando en palabra-función o no
    alfabético). 'juan david wilchez, ...' → 'Juan David Wilchez'."""
    tokens: list[str] = []
    for raw in re.split(r"[\s,;]+", texto or ""):
        t = raw.lower().strip(",.;¿?¡!()")
        if not t:
            continue
        if t in _PREFIJO_CORTESIA:
            continue
        if t in _PALABRAS_NO_NOMBRE or not re.match(r"^[a-záéíóúñ]+$", t):
            break
        tokens.append(t)
        if len(tokens) >= max_tokens:
            break
    if not tokens:
        return None
    nombre = " ".join(w[:1].upper() + w[1:] for w in tokens)
    return nombre if _es_nombre_valido(nombre) else None


# Marcador de hijo para el BUNDLE: como _NOMBRE_HIJO_MARCADOR_RE pero con "mi"
# OPCIONAL, para pescar "hijo X" pelón ('maria urdaneta, hijo juan david wilchez').
_BUNDLE_HIJO_MARCADOR_RE = re.compile(
    r"\b(?:"
    r"se\s+llama|"
    r"(?:mi\s+)?(?:hijo|hija|peque\w*|niñ[oa]|nin[oa]|beb[eé])(?:\s+(?:se\s+llama|es))?|"
    r"el\s+niñ[oa]|la\s+niñ[oa]"
    r")\s+",
    re.IGNORECASE,
)


def parsear_bundle_papa_hijo(mensaje: str) -> tuple[str | None, str | None]:
    """ALCANCE ACOTADO (lunes): parsea el patrón bundle 'X, hijo Y' donde el papá
    dio AMBOS nombres en un turno (caso real de María: 'maria urdaneta, hijo juan
    david wilchez'). El marcador de hijo PARTE el mensaje: lo de antes → papá; lo
    de después → hijo. Devuelve (nombre_papa, nombre_hijo); cada uno puede ser None.

    NO reemplaza los guards (eso es el refactor fast-follow); solo rescata el bundle
    que hoy se cae por los 4 guards a la vez.
    """
    m = _BUNDLE_HIJO_MARCADOR_RE.search(mensaje or "")
    if not m:
        return None, None
    antes = (mensaje or "")[: m.start()]
    despues = (mensaje or "")[m.end() :]
    hijo = _tomar_nombre_inicial(despues, max_tokens=3)
    # El papá es el nombre limpio ANTES del marcador (sin email/tel), o una
    # presentación explícita DESPUÉS ('... soy María').
    papa = _nombre_limpio_de_respuesta(antes) or extraer_nombre_papa(despues)
    return papa, hijo


# Presentación EXPLÍCITA del papá ("yo soy X", "me llamo X", "mi nombre es X").
# FIX (e): habilita corregir un nombre_papa clavado de una sesión contaminada.
_PRESENTACION_RE = re.compile(
    r"(?:^|[\s,.;])(?:yo\s+soy|me\s+llamo|mi\s+nombre\s+es|soy)\s+[a-záéíóúñ]",
    re.IGNORECASE,
)


def _es_presentacion_explicita(mensaje: str) -> bool:
    return bool(_PRESENTACION_RE.search(mensaje or ""))


class ExtraccionTurno(BaseModel):
    """Output del extractor. Cualquier campo puede ser None si no se detectó."""

    nombre_papa: str | None = None
    # FIX (e) 2026-06-01: True si el papá se presentó EXPLÍCITAMENTE ("yo soy X",
    # "me llamo X"). Permite corregir un nombre_papa mal asignado/clavado de una
    # sesión contaminada (no se persiste; es señal de merge para aplicar_extraccion).
    nombre_papa_explicito: bool = False
    email_papa: str | None = None  # D.3 (Lily 2026-05-27): captura pre-cita
    telefono: str | None = None  # D.3: número celular del papá
    nivel_buscado: str | None = None  # 'maternal'|'kinder'|'primaria'|'secundaria'|None
    nombre_hijo: str | None = None
    edad_hijo: int | None = Field(default=None, ge=0, le=20)
    # Fix B.1 (2026-05-19): campo separado para evitar que "tengo 4 hijos"
    # se interprete como "edad_hijo=4". Si el papá dice una cantidad de hijos,
    # va aquí; edad_hijo queda null hasta que se mencione "X años / añitos".
    cantidad_hijos: int | None = Field(default=None, ge=0, le=10)
    grado_hijo: str | None = None
    escuela_actual: str | None = None
    diagnostico_hijo: str | None = None
    miedos_nuevos: list[str] = Field(default_factory=list)
    resono_con_nuevos: list[str] = Field(default_factory=list)
    objeciones_nuevas: list[str] = Field(default_factory=list)
    pidio_costos: bool = False
    vive_fuera_saltillo: bool = False
    quiere_agendar: bool = False


_SYSTEM_PROMPT = """Eres un extractor de información para Sofía, agente de admisiones de Maple Collège.

Recibes:
1. El estado YA CAPTURADO del papá (datos previos en JSON).
2. El último mensaje del papá.

Tu tarea: detectar datos NUEVOS que aparezcan en el mensaje. Si un dato ya está en el estado, NO lo repitas. Si no detectas nada nuevo en algún campo, déjalo como null o lista vacía.

Reglas:
- "nombre_papa": el nombre propio del papá/mamá cuando se presenta. Detecta patrones como: "Me llamo X", "Soy X", "Mi nombre es X", "Hola, soy X", "Habla X", o cuando firma con su nombre al final ("Saludos, X"). Toma SOLO el nombre y apellido(s) (NO incluyas titulos como "Sr.", "Sra.", "Dr."). Si el papá menciona el nombre del HIJO, eso va en "nombre_hijo", NO aquí. Ver ejemplos few-shot abajo.
- "email_papa": email del papá si aparece (formato `algo@dominio.tld`). Captura literal, sin cambiar mayúsculas. Si no aparece, null.
- "telefono": número celular del papá si aparece. Acepta formatos: "8441234567", "844 123 4567", "+52 844 123 4567", "844-123-45-67". Captura solo dígitos + signo +, sin espacios ni guiones (normaliza). Mínimo 10 dígitos. Si no aparece, null.
- "nivel_buscado": SOLO uno de: maternal, kinder, primaria, secundaria. Mapea variantes naturales: "2do de primaria"→primaria, "primero kinder"→kinder, "preescolar"→kinder, "secu"→secundaria, "mater"→maternal.
- "edad_hijo": número entero entre 0 y 20 — SOLO cuando el papá habla explícitamente de **EDAD** (verbo "tener", palabras "años", "añitos", "meses", "cumplió"). Ver reglas de desambiguación abajo.
- "cantidad_hijos": número entero entre 0 y 10 — SOLO cuando el papá menciona **CUÁNTOS HIJOS** tiene (no la edad). Ver reglas de desambiguación abajo.
- "grado_hijo": tal como lo dijo el papá ("2do primaria", "1ro kinder", etc.).
- "diagnostico_hijo": SOLO si el papá menciona explícitamente un diagnóstico (autismo, TDAH, etc.). Si no, null.
- "miedos_nuevos": ej. "bullying", "que no aprenda", "falta de disciplina". Lista corta de etiquetas.
- "resono_con_nuevos": ideas que parecieron resonarle ("le gustó la metodología", "le importó el vínculo").
- "objeciones_nuevas": objeciones explícitas ("está caro", "no tienen tarea", "es muy flexible").
- "pidio_costos": true SOLO si pregunta directamente por precio/costo/colegiatura.
- "vive_fuera_saltillo": true si menciona que no vive en Saltillo o va a mudarse.
- "quiere_agendar": true si pide cita, visita, conocer el colegio explícitamente.

## Desambiguación CRÍTICA: cantidad de hijos vs edad del hijo

Bug detectado en reunión Maple 2026-05-19: el papá decía "tengo cuatro hijos" y Sofía interpretaba que el hijo tenía 4 años. Reglas estrictas:

**Va a `cantidad_hijos` (NO a `edad_hijo`):**
- "tengo N hijos" / "somos N hijos" / "son N (hijos/niños)" / "tengo N niños/niñas"
- "tengo dos niños y una niña" → cantidad_hijos=3
- Cualquier frase donde el número se refiere al CONTEO de hijos, no a años.

**Va a `edad_hijo` (NO a `cantidad_hijos`):**
- "mi hijo tiene N años / añitos / meses"
- "él tiene N años" / "ella tiene N"
- "ya cumplió N" / "N años cumplidos"
- "es de N años" / "tiene N"
- Cualquier frase donde el número se refiere a la EDAD.

**Ambiguo (deja ambos en null — Sofía preguntará):**
- "N" solo, sin verbo ni contexto ("4", "cuatro").
- "X niños" sin verbo de posesión ("muchos niños", "varios").

## Ejemplos few-shot

Mensaje: "tengo cuatro hijos"
Output: {"cantidad_hijos": 4, "edad_hijo": null, ...}

Mensaje: "somos 3 hijos en la familia"
Output: {"cantidad_hijos": 3, "edad_hijo": null, ...}

Mensaje: "tengo 2 niños y 1 niña"
Output: {"cantidad_hijos": 3, "edad_hijo": null, ...}

Mensaje: "mi hijo tiene 4 años"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "él tiene 4 añitos"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "ya cumplió 5"
Output: {"cantidad_hijos": null, "edad_hijo": 5, ...}

Mensaje: "es de 4 años"
Output: {"cantidad_hijos": null, "edad_hijo": 4, ...}

Mensaje: "4"
Output: {"cantidad_hijos": null, "edad_hijo": null, ...}

Mensaje: "cuatro"
Output: {"cantidad_hijos": null, "edad_hijo": null, ...}

## Ejemplos few-shot — nombre_papa

Mensaje: "Me llamo Oscar Rodriguez"
Output: {"nombre_papa": "Oscar Rodriguez", ...}

Mensaje: "Soy Ana, busco info para mi hijo"
Output: {"nombre_papa": "Ana", "quiere_agendar": false, ...}

Mensaje: "Hola, soy Juan Carlos Pérez"
Output: {"nombre_papa": "Juan Carlos Pérez", ...}

Mensaje: "Mi nombre es Maria Elena"
Output: {"nombre_papa": "Maria Elena", ...}

Mensaje: "Me llamo Oscar Rodriguez, busco kinder para mi hijo de 5 años"
Output: {"nombre_papa": "Oscar Rodriguez", "nivel_buscado": "kinder", "edad_hijo": 5, ...}

Mensaje: "habla la mamá de Lucía"
Output: {"nombre_papa": null, "nombre_hijo": "Lucía", ...}

Mensaje: "mi hijo Diego está en 2do de primaria"
Output: {"nombre_papa": null, "nombre_hijo": "Diego", "grado_hijo": "2do de primaria", "nivel_buscado": "primaria", ...}

Mensaje: "Jose, 4 años"
Output: {"nombre_hijo": "Jose", "edad_hijo": 4, ...}

Mensaje: "se llama Ana y tiene 3 añitos"
Output: {"nombre_hijo": "Ana", "edad_hijo": 3, ...}

Mensaje: "2 kinder"
Output: {"grado_hijo": "2° de Kinder", "nivel_buscado": "kinder", ...}

Mensaje: "va en kinder 3"
Output: {"grado_hijo": "3° de Kinder", "nivel_buscado": "kinder", ...}

Mensaje: "Hola"
Output: {"nombre_papa": null, ...}

## Ejemplos few-shot — email_papa y telefono (D.3 — Lily 2026-05-27)

Mensaje: "Mi correo es oscar@example.com"
Output: {"email_papa": "oscar@example.com", ...}

Mensaje: "Soy Oscar, mi número es 8441234567"
Output: {"nombre_papa": "Oscar", "telefono": "8441234567", ...}

Mensaje: "Mi celular es +52 844 123 4567 y mi correo ana.perez@gmail.com"
Output: {"telefono": "+528441234567", "email_papa": "ana.perez@gmail.com", ...}

Mensaje: "844-123-45-67"
Output: {"telefono": "8441234567", ...}

Mensaje: "te paso mi info: María López, 844 555 1212, maria@correo.mx"
Output: {"nombre_papa": "María López", "telefono": "8445551212", "email_papa": "maria@correo.mx", ...}

Devuelve EXCLUSIVAMENTE JSON con la estructura de ExtraccionTurno.
"""


async def extraer_de_mensaje(
    mensaje: str,
    estado_actual: EstadoCapturado,
    *,
    ultimo_assistant: str | None = None,
) -> ExtraccionTurno:
    """Extrae datos nuevos del último mensaje del papá.

    `ultimo_assistant`: último turno de Sofía, para capturar respuestas SUELTAS
    por contexto (p.ej. el nombre del papá tras "¿tu nombre?").

    No mergea — eso lo hace el caller con `aplicar_extraccion()`.
    """
    openai = get_openai()
    if not openai.is_configured():
        log.warning("openai not configured, solo extracción determinística")
        result = ExtraccionTurno()
    else:
        estado_json = estado_actual.model_dump_json(exclude_defaults=True)
        user_text = (
            f"ESTADO YA CAPTURADO:\n{estado_json}\n\n"
            f"ÚLTIMO MENSAJE DEL PAPÁ:\n{mensaje}\n\n"
            f"Detecta SOLO datos NUEVOS que no estén ya en el estado."
        )
        try:
            raw = await openai.classify(text=user_text, instructions=_SYSTEM_PROMPT)
        except Exception as exc:
            log.warning("state_extractor api error", extra={"error": str(exc)})
            result = ExtraccionTurno()
        else:
            result = _parse_extraction(raw)

    return _aplicar_fallbacks_deterministicos(
        result,
        mensaje,
        ultimo_assistant=ultimo_assistant,
        ultimo_campo_pedido=estado_actual.ultimo_campo_pedido,
    )


def _aplicar_fallbacks_deterministicos(
    result: ExtraccionTurno,
    mensaje: str,
    *,
    ultimo_assistant: str | None = None,
    ultimo_campo_pedido: str | None = None,
) -> ExtraccionTurno:
    """Capa de captura DETERMINÍSTICA consolidada (FIX 2026-06-02).

    El LLM NO es load-bearing: corre SIEMPRE (incluso si el LLM no estaba
    disponible). Los extractores de alta confianza (email, teléfono, "yo soy X")
    MANDAN sobre el LLM; los demás rellenan lo que el LLM dejó vacío. Además sanea
    nombres inválidos ('pequeño', 'tiene') para que el gate los pida, no se inventen.
    """
    # --- Email y teléfono: regex MANDA (datos no ambiguos). ---
    email_det = extraer_email(mensaje)
    if email_det:
        result.email_papa = email_det
    tel_det = extraer_telefono(mensaje, excluir=email_det)
    if tel_det:
        result.telefono = tel_det

    # --- Bundle "X, hijo Y" (papá + hijo en un turno): parse acotado (caso María).
    # El marcador de hijo parte el mensaje; lo de antes → papá. Rescata lo que hoy
    # se cae por los 4 guards a la vez. ---
    bundle_papa, bundle_hijo = parsear_bundle_papa_hijo(mensaje)

    # --- Nombre del papá: presentación explícita ("yo soy X"), respuesta SUELTA
    # a "¿tu nombre?" (ultimo_campo_pedido) o el papá del bundle. MANDAN + marcan
    # flag → la captura NO depende de cómo lo frasee Haiku. ---
    nombre_papa_det = (
        extraer_nombre_papa(mensaje)
        or nombre_papa_por_contexto(mensaje, ultimo_assistant)
        or bundle_papa
    )
    if not nombre_papa_det and ultimo_campo_pedido == "nombre_papa":
        # El CÓDIGO pidió el nombre del papá el turno anterior → la respuesta suelta
        # ("Oscar Rodriguez", admite tel/correo junto) es su nombre, salvo que sea
        # presentación del hijo.
        if not (extraer_nombre_hijo(mensaje) or _nombre_junto_a_edad(mensaje)):
            nombre_papa_det = _nombre_limpio_de_respuesta(mensaje)
    if nombre_papa_det:
        result.nombre_papa = nombre_papa_det
        result.nombre_papa_explicito = True
    elif result.nombre_papa and _es_presentacion_explicita(mensaje):
        result.nombre_papa_explicito = True

    # --- Edad: regex 'N años' rellena si el LLM la dejó vacía. Si el gate pidió la
    # EDAD, un número SUELTO ("5", "tiene 5") es la edad. ---
    if result.edad_hijo is None:
        edad_det = extraer_edad_simple(mensaje)
        if edad_det is None and ultimo_campo_pedido == "edad":
            edad_det = extraer_edad_de_numero_suelto(mensaje)
        if edad_det is not None:
            result.edad_hijo = edad_det

    # --- Grado: "2 kinder" → "2° de Kinder" cuando el LLM lo dejó en None. ---
    if not result.grado_hijo:
        grado_det, nivel_det = extraer_grado_simple(mensaje)
        if grado_det:
            result.grado_hijo = grado_det
            if not result.nivel_buscado and nivel_det:
                result.nivel_buscado = nivel_det
    # CANONICALIZAR el grado declarado (FIX 2026-06-04): "primero de primaria" →
    # "1° de Primaria". Así un grado que el papá DECLARA en palabra queda canónico
    # y la derivación por edad NO lo pisa (Política A: el grado declarado manda).
    if result.grado_hijo:
        g_canon, niv_canon = extraer_grado_simple(result.grado_hijo)
        if g_canon:
            result.grado_hijo = g_canon
            if not result.nivel_buscado and niv_canon:
                result.nivel_buscado = niv_canon

    # --- Nombre del hijo: "Jose, 4 años" (pegado a edad), "se llama Emanuel"
    # (marcador) o "Emanuel Rodriguez" SUELTO tras "¿nombre de tu hijo?" (contexto)
    # → es el NIÑO, no el papá. ---
    nombre_nino = (
        _nombre_junto_a_edad(mensaje)
        or extraer_nombre_hijo(mensaje)
        or bundle_hijo
        or nombre_hijo_por_contexto(mensaje, ultimo_assistant)
    )
    if not nombre_nino and ultimo_campo_pedido == "nombre_hijo":
        # El CÓDIGO pidió el nombre del hijo el turno anterior → la respuesta suelta
        # ("Emanuel Rodriguez") es el nombre del niño, sin depender de Haiku.
        nombre_nino = _nombre_limpio_de_respuesta(mensaje)
    if nombre_nino:
        if not result.nombre_hijo:
            result.nombre_hijo = nombre_nino
        # Si el LLM lo asignó como nombre del papá, corrígelo (era el niño) —
        # salvo que el papá se haya presentado explícitamente en ESTE mensaje.
        if (
            result.nombre_papa
            and result.nombre_papa.strip().lower() == nombre_nino.lower()
            and not extraer_nombre_papa(mensaje)
        ):
            result.nombre_papa = None

    # --- Saneo de nombres: descarta palabras comunes ('pequeño', 'tiene', 'bebé')
    # que NO son nombres propios → el gate los pedirá, NO se inventan. ---
    if result.nombre_hijo and not _es_nombre_valido(result.nombre_hijo):
        result.nombre_hijo = None
    if result.nombre_papa and not _es_nombre_valido(result.nombre_papa):
        result.nombre_papa = None

    # --- El nombre del papá SOLO entra por su propia vía: presentación explícita
    # ("yo soy/me llamo X") o respuesta a "¿tu nombre?". Una conjetura del LLM (no
    # explícita) se DESCARTA: así el apellido/nombre del hijo ("Emanuel Rodriguez")
    # NUNCA sangra al slot del papá y el gate SIEMPRE pregunta "¿y tu nombre?". ---
    if result.nombre_papa and not result.nombre_papa_explicito:
        result.nombre_papa = None

    return result


def _parse_extraction(raw: str) -> ExtraccionTurno:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data: dict[str, Any] = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("state_extractor non-json", extra={"raw": raw[:200], "err": str(exc)})
        return ExtraccionTurno()

    try:
        return ExtraccionTurno.model_validate(data)
    except Exception as exc:
        log.warning("state_extractor invalid schema", extra={"data": data, "err": str(exc)})
        return ExtraccionTurno()


def aplicar_extraccion(
    estado_actual: EstadoCapturado,
    extraccion: ExtraccionTurno,
) -> EstadoCapturado:
    """Aplica los datos nuevos al estado existente (merge defensivo).

    Reglas de merge:
    - Strings nuevos sobrescriben SOLO si el actual es None.
    - Booleans true se "sticky" — no se reescriben a false.
    - Listas se acumulan (sin duplicados).
    - Si aparece nivel/nombre/edad de hijo, se agrega o actualiza HijoInfo.
    """
    nuevo = estado_actual.model_copy(deep=True)

    # FIX (c) 2026-06-01: si el nombre clavado en nombre_papa resulta ser el del
    # hijo (entró en el slot equivocado en un turno previo), libera el slot para
    # que el nombre real del papá pueda entrar después. Evita "Jose" (niño) clavado
    # como papá impidiendo a "Oscar" registrarse.
    if (
        extraccion.nombre_hijo
        and nuevo.nombre_papa
        and extraccion.nombre_hijo.strip().lower() == nuevo.nombre_papa.strip().lower()
    ):
        nuevo.nombre_papa = None

    # FIX (e): un nombre explícito ("yo soy Oscar") SOBREESCRIBE aunque ya haya uno
    # clavado (corrige contaminación). Si no es explícito, solo llena si está vacío.
    if extraccion.nombre_papa and (not nuevo.nombre_papa or extraccion.nombre_papa_explicito):
        nuevo.nombre_papa = extraccion.nombre_papa

    if extraccion.email_papa and not nuevo.email_papa:
        nuevo.email_papa = extraccion.email_papa

    if extraccion.telefono and not nuevo.telefono:
        nuevo.telefono = extraccion.telefono

    if extraccion.pidio_costos:
        nuevo.pidio_costos = True

    if extraccion.vive_fuera_saltillo:
        nuevo.vive_fuera_saltillo = True

    # Acumular listas con dedup preservando orden
    for miedo in extraccion.miedos_nuevos:
        if miedo and miedo not in nuevo.miedos:
            nuevo.miedos.append(miedo)

    for resono in extraccion.resono_con_nuevos:
        if resono and resono not in nuevo.resono_con:
            nuevo.resono_con.append(resono)

    for obj in extraccion.objeciones_nuevas:
        if obj and obj not in nuevo.objeciones_planteadas:
            nuevo.objeciones_planteadas.append(obj)

    # Actualizar/crear info de hijo si el extractor detectó algo
    if extraccion.nivel_buscado:
        try:
            nivel_enum = NivelEducativo(extraccion.nivel_buscado.lower())
        except ValueError:
            nivel_enum = None
        if nivel_enum:
            nuevo.nivel_buscado_actual = nivel_enum
            # Sincroniza con el (primer) hijo si no hay info
            _upsert_hijo(
                nuevo,
                nivel=nivel_enum,
                nombre=extraccion.nombre_hijo,
                edad=extraccion.edad_hijo,
                grado=extraccion.grado_hijo,
                escuela_actual=extraccion.escuela_actual,
                diagnostico=extraccion.diagnostico_hijo,
            )
    elif (
        extraccion.nombre_hijo
        or extraccion.edad_hijo is not None
        or extraccion.grado_hijo
        or extraccion.escuela_actual
        or extraccion.diagnostico_hijo
    ):
        _upsert_hijo(
            nuevo,
            nivel=None,
            nombre=extraccion.nombre_hijo,
            edad=extraccion.edad_hijo,
            grado=extraccion.grado_hijo,
            escuela_actual=extraccion.escuela_actual,
            diagnostico=extraccion.diagnostico_hijo,
        )

    return nuevo


def _upsert_hijo(
    estado: EstadoCapturado,
    *,
    nivel: NivelEducativo | None,
    nombre: str | None,
    edad: int | None,
    grado: str | None,
    escuela_actual: str | None,
    diagnostico: str | None,
) -> None:
    """Actualiza el primer hijo cuyo nivel coincida, o crea uno nuevo."""
    target: HijoInfo | None = None
    if nivel is not None:
        for h in estado.hijos:
            if h.nivel == nivel:
                target = h
                break
    if target is None and estado.hijos and nivel is None:
        target = estado.hijos[0]
    if target is None:
        target = HijoInfo(nivel=nivel)
        estado.hijos.append(target)

    if nivel and not target.nivel:
        target.nivel = nivel
    if nombre and not target.nombre:
        target.nombre = nombre
    if edad is not None and target.edad is None:
        target.edad = edad
    if grado and not target.grado:
        target.grado = grado
    if escuela_actual and not target.escuela_actual:
        target.escuela_actual = escuela_actual
    if diagnostico and not target.diagnostico:
        target.diagnostico = diagnostico
