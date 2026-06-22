"""Resuelve, desde el estado de la conversación, el nivel/sub-nivel exacto para
inyectar el dato estructurado correcto de costos / horarios / estancias.

Reglas:
- COSTOS (precios_por_nivel): granularidad por NIVEL —
  'kinder' (mismo precio para sus 3 grados), 'maternal', 'primaria_baja' (1-3),
  'primaria_alta' (4-6), 'secundaria'. Si no se puede inferir 1-3 vs 4-6, None.
- HORARIOS (horarios_por_nivel): granularidad por SUB-NIVEL — kinder tiene 3
  horarios distintos (kinder_1/2/3); se resuelve por grado. Si es kinder sin grado
  → (None, necesita_grado=True): el caller pide el grado.
- ESTANCIAS: usa el nivel de precios ('kinder'|'maternal'|'primaria_baja'|...).
"""

from __future__ import annotations

import re

from app.core.campus_resolver import (
    _infer_grado_kinder,
    _infer_grado_primaria,
    _infer_grado_secundaria,
)
from app.core.state import EstadoConversacion, NivelEducativo

# Nivel mencionado SUELTO en el mensaje ("para kinder, costos"). El LLM/extracción a
# veces no lo captura en frases cortas → este respaldo determinístico sí.
_NIVEL_MSG: list[tuple] = [
    (
        re.compile(
            r"\b(?:maternal|early\s*years|guarder[íi]a|toddlers?|infants?|babies|baby|cubs?)\b",
            re.IGNORECASE,
        ),
        NivelEducativo.MATERNAL,
    ),
    (
        re.compile(r"\b(?:kinder|k[íi]nder|preescolar|preschool)\b", re.IGNORECASE),
        NivelEducativo.KINDER,
    ),
    (re.compile(r"\b(?:secundaria|secu)\b", re.IGNORECASE), NivelEducativo.SECUNDARIA),
    (re.compile(r"\bprimaria\b", re.IGNORECASE), NivelEducativo.PRIMARIA),
]


def nivel_buscado_de_mensaje(mensaje: str) -> NivelEducativo | None:
    """Nivel mencionado en el mensaje ('para kinder' → KINDER). None si no hay."""
    m = mensaje or ""
    for rx, nivel in _NIVEL_MSG:
        if rx.search(m):
            return nivel
    return None


# Grado SUELTO ("3", "1°", "tercero", "4to", "1 a 3") → para la rama de horarios cuando
# ya se pidió el grado. Mapea al rango/grado válido según el nivel.
_GRADO_PALABRA = {
    "primero": 1,
    "primer": 1,
    "segundo": 2,
    "tercero": 3,
    "tercer": 3,
    "cuarto": 4,
    "quinto": 5,
    "sexto": 6,
}
_GRADO_NUM_RE = re.compile(r"\b([1-9])\s*(?:°|º|ro|do|er|to|vo|mo)?\b")
_RANGO_BAJA_RE = re.compile(r"\b1\s*(?:a|-|al)\s*3\b")
_RANGO_ALTA_RE = re.compile(r"\b4\s*(?:a|-|al)\s*6\b")
_MAX_GRADO = {"kinder": 3, "primaria": 6, "secundaria": 3, "maternal": 0}
_DISPLAY_NIVEL = {"kinder": "Kinder", "primaria": "Primaria", "secundaria": "Secundaria"}


def extraer_grado_suelto(mensaje: str, nivel: NivelEducativo | None) -> str | None:
    """'3'/'1°'/'tercero'/'4to'/'1 a 3' → '3° de Primaria' (canónico, según `nivel`).
    None si no parsea o no es un grado válido para ese nivel."""
    if nivel is None:
        return None
    nivel_val = nivel.value if hasattr(nivel, "value") else str(nivel)
    display = _DISPLAY_NIVEL.get(nivel_val)
    if display is None:
        return None
    t = (mensaje or "").lower()
    g: int | None = None
    if _RANGO_BAJA_RE.search(t):
        g = 1
    elif _RANGO_ALTA_RE.search(t):
        g = 4
    else:
        for pal, n in _GRADO_PALABRA.items():
            if re.search(rf"\b{pal}\b", t):
                g = n
                break
        if g is None:
            m = _GRADO_NUM_RE.search(t)
            if m:
                g = int(m.group(1))
    if g is None or not (1 <= g <= _MAX_GRADO.get(nivel_val, 0)):
        return None
    return f"{g}° de {display}"


# ============================================================
# Detección DETERMINÍSTICA de consultas de oferta (keywords) — NO depende del
# clasificador LLM (que mandó "kinder, costos y horarios" a confuso_otro).
# ============================================================

_COSTOS_RE = re.compile(
    # SOLO palabras de COSTO. Antes incluía "informes/información" (pedido de DATO →
    # emitía precios), pero "información del colegio" disparaba la tabla de precios de la
    # nada (queja de Gaby). La KB es clara: NO compartir costos salvo que los pidan
    # EXPLÍCITO. "información" ahora la maneja Haiku como pregunta general.
    r"\b(?:costos?|cuestan?|precios?|colegiaturas?|mensualidad(?:es)?|"
    r"inscripci[óo]n(?:es)?|cu[áa]nto\s+(?:cuesta|sale|es|pagar|pago|vale))\b",
    re.IGNORECASE,
)
# "horario" escolar; "horario EXTENDIDO" NO es horario escolar → es estancia (no mezclar).
_HORARIO_RE = re.compile(
    r"\bhorarios?\b(?!\s+extendid)|\ba\s+qu[ée]\s+hora\b|\bqu[ée]\s+hora(?:rio)?\b(?!\s+extendid)|"
    r"\bhora\s+de\s+(?:entrada|salida)\b",
    re.IGNORECASE,
)
_ESTANCIAS_RE = re.compile(
    r"\bestancias?\b|\bhorario\s+extendid|\bjornada\s+extendid|\bafter\s*school\b|"
    r"\bguarder[íi]a\b|\bextraescolar",
    re.IGNORECASE,
)


def detectar_consulta_oferta(mensaje: str) -> set[str]:
    """Devuelve qué tipos de dato pide el mensaje: {'costos','horario','estancias'}.
    Determinístico (palabras clave), independiente del clasificador LLM."""
    m = mensaje or ""
    tipos: set[str] = set()
    if _COSTOS_RE.search(m):
        tipos.add("costos")
    if _HORARIO_RE.search(m):
        tipos.add("horario")
    if _ESTANCIAS_RE.search(m):
        tipos.add("estancias")
    return tipos


# ============================================================
# Guard de salida: el número lo EMITE el código; cualquier $monto u hora que
# Haiku escriba y NO esté en el bloque oficial se ELIMINA antes de salir.
# ============================================================

_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*)")
_HHMM_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# Hora con meridiano SIN minutos ("8 a.m."), no precedida por ':' o dígito.
_HORA_AMPM_RE = re.compile(r"(?<![:\d])(\d{1,2})\s?[ap]\.?\s?m\.?\b", re.IGNORECASE)


def extraer_figuras(texto: str) -> set[str]:
    """Normaliza montos y horas a tokens comparables: '$5,250'→'5250', '9:00'→'9:00',
    '8 a.m.'→'8:00'."""
    s: set[str] = set()
    for mm in _MONEY_RE.finditer(texto or ""):
        s.add(mm.group(1).replace(",", ""))
    for mm in _HHMM_RE.finditer(texto or ""):
        s.add(f"{int(mm.group(1))}:{mm.group(2)}")
    for mm in _HORA_AMPM_RE.finditer(texto or ""):
        s.add(f"{int(mm.group(1))}:00")
    return s


def sanear_cifras_ajenas(texto_haiku: str, permitidas: set[str]) -> str:
    """Elimina de la parte de Haiku cada LÍNEA que contenga un $monto u hora que NO
    esté en `permitidas` (las que emitió el código). El dato correcto ya va aparte;
    así, aunque Haiku escriba '$6,450' u '8:00', se borra antes de salir."""
    fuera: list[str] = []
    for linea in (texto_haiku or "").split("\n"):
        figs = extraer_figuras(linea)
        if figs and not figs <= permitidas:
            continue  # la línea tiene una cifra NO oficial → se elimina
        fuera.append(linea)
    # Colapsa líneas en blanco repetidas que pudieran quedar tras borrar.
    out = "\n".join(fuera)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _nivel_edad_grado(estado: EstadoConversacion) -> tuple[str | None, int | None, str | None]:
    capt = estado.estado_capturado
    nivel_enum = capt.nivel_buscado_actual
    edad: int | None = None
    grado: str | None = None
    h = capt.hijo_efectivo()
    if h is not None:
        if nivel_enum is None:
            nivel_enum = h.nivel
        edad = h.edad
        grado = h.grado
    nivel = nivel_enum.value if nivel_enum is not None else None
    return nivel, edad, grado


def precio_nivel_de_estado(estado: EstadoConversacion) -> str | None:
    """→ 'kinder'|'maternal'|'primaria_baja'|'primaria_alta'|'secundaria' o None
    (None = sin nivel claro, o primaria sin grado para distinguir baja/alta)."""
    nivel, edad, grado = _nivel_edad_grado(estado)
    if nivel in ("maternal", "kinder", "secundaria"):
        return nivel
    if nivel == "primaria":
        g = _infer_grado_primaria(edad=edad, grado_texto=grado)
        if g is None:
            return None
        return "primaria_baja" if g <= 3 else "primaria_alta"
    return None


def horario_subnivel_de_estado(estado: EstadoConversacion) -> tuple[str | None, bool]:
    """→ (subnivel, necesita_grado). subnivel: 'kinder_1'..'secundaria'|'maternal'.
    necesita_grado=True cuando es kinder o primaria sin grado claro (el horario
    depende del grado exacto)."""
    nivel, edad, grado = _nivel_edad_grado(estado)
    if nivel == "maternal":
        return "maternal", False
    if nivel == "kinder":
        g = _infer_grado_kinder(edad=edad, grado_texto=grado)
        if g is None:
            return None, True  # kinder tiene 3 horarios → hay que pedir el grado
        return f"kinder_{g}", False
    if nivel == "primaria":
        g = _infer_grado_primaria(edad=edad, grado_texto=grado)
        if g is None:
            return None, True
        return ("primaria_baja" if g <= 3 else "primaria_alta"), False
    if nivel == "secundaria":
        _infer_grado_secundaria(edad=edad, grado_texto=grado)  # un solo horario
        return "secundaria", False
    return None, False
