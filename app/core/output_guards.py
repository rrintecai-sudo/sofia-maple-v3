"""Guards de salida sobre el TEXTO LIBRE de Haiku (nunca sobre líneas emitidas por
código: oferta, pregunta de colección, cierre D.4).

Misma lección que `sanear_cifras_ajenas`: ENFORZAR, no instruir. Aunque el prompt le
pida a Haiku tono cerrado y una sola pregunta, Haiku lo ignora — así que el código
elimina venezolanismos/colombianismos y recorta preguntas de más antes de salir.
"""

from __future__ import annotations

import re

# ============================================================
# Lista AMPLIABLE por Lili (devs agregan). Patrones regex, case-insensitive.
# Si una frase aparece en una oración, esa oración se elimina completa.
# ============================================================
FRASES_PROHIBIDAS: list[str] = [
    r"¿?\s*c[oó]mo\s+lo\s+viven\b",            # venezolanismo: "¿cómo lo viven?"
    r"\bte\s+vien[e]?n?\s+(?:bien|mejor)\b",   # "te viene/vienen bien/mejor"
    r"\bregalad[oa]s?\b",                       # "precio regalado/regalada"
    r"\bch[ée]vere\b",                          # venezolanismo
    r"\bde\s+pinga\b",                          # venezolanismo
]

# Máximo de preguntas en el texto de Haiku. Configurable (subir a 2 si se decide).
MAX_PREGUNTAS_POR_TURNO = 1

# Sondeo (afirmaciones que pescan info del papá). En turno de OFERTA no debe ir
# NINGÚN sondeo — ni pregunta ni afirmación. Lista ampliable.
SONDEO_AFIRMATIVO: list[str] = [
    r"\bme\s+(?:gustar[íi]a|encantar[íi]a)\s+(?:entender|saber|conocer)\b",
    r"\b(?:cu[ée]ntame|cu[ée]ntanos|plat[íi]came|plat[íi]canos)\b",
    r"\bquiero\s+(?:entender|saber|conocer)\s+(?:qu[ée]|c[óo]mo|de\s+ti|m[áa]s\s+de)\b",
    r"\bqu[ée]\s+(?:es\s+lo\s+que\s+)?(?:m[áa]s\s+)?(?:te\s+importa|buscas|esperas|necesitas)\b",
    r"\bay[úu]dame\s+a\s+(?:entender|conocer)\b",
]

_FRASES_COMPILADAS = [re.compile(p, re.IGNORECASE) for p in FRASES_PROHIBIDAS]
_SONDEO_COMPILADO = [re.compile(p, re.IGNORECASE) for p in SONDEO_AFIRMATIVO]


def limpiar_marcadores_sueltos(texto: str) -> str:
    """Quita marcadores de formato huérfanos (un '**' sin par) que quedan cuando se
    elimina una oración a la mitad de un texto en negritas. Evita que salga '**'
    suelto al usuario. Conserva las negritas BIEN formadas ('**texto**')."""
    out: list[str] = []
    for linea in (texto or "").split("\n"):
        if linea.count("**") % 2 == 1:  # número impar → hay un huérfano
            idx = linea.rfind("**")
            linea = (linea[:idx] + linea[idx + 2:]).rstrip()
        out.append(linea)
    txt = "\n".join(out)
    return re.sub(r"[ \t]{2,}", " ", txt).strip()


def sanear_sondeo(texto: str) -> str:
    """Elimina ORACIONES de sondeo afirmativo ('me gustaría entender qué buscas...').
    Para usar en turnos de OFERTA: da el dato y para, sin pescar info."""
    segmentos = _segmentar(texto)
    fuera = [s for s in segmentos if not any(p.search(s) for p in _SONDEO_COMPILADO)]
    return _rejoin(fuera)


def _segmentar(texto: str) -> list[str]:
    """Parte el texto en segmentos (oraciones) preservando puntuación y saltos de
    línea, para poder quitar una oración completa sin romper el resto."""
    if not texto:
        return []
    # Cada match: texto hasta un terminador .!? (con sus repeticiones) + espacios,
    # o un salto de línea suelto, o el resto final sin terminador.
    return re.findall(r"[^.!?\n]*[.!?]+[\s]*|\n|[^.!?\n]+$", texto)


def _rejoin(segmentos: list[str]) -> str:
    out = "".join(segmentos)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def sanear_frases_prohibidas(
    texto: str, patrones: list[re.Pattern] | None = None
) -> str:
    """Elimina cada ORACIÓN que contenga una frase prohibida (venezolanismo/etc).
    SOLO debe llamarse sobre el texto libre de Haiku."""
    pats = patrones if patrones is not None else _FRASES_COMPILADAS
    segmentos = _segmentar(texto)
    fuera = [s for s in segmentos if not any(p.search(s) for p in pats)]
    return _rejoin(fuera)


def limitar_preguntas(texto: str, maximo: int = MAX_PREGUNTAS_POR_TURNO) -> str:
    """Conserva las primeras `maximo` oraciones-pregunta y elimina las demás
    PREGUNTAS (las oraciones afirmativas se conservan). SOLO sobre texto de Haiku."""
    segmentos = _segmentar(texto)
    vistas = 0
    fuera: list[str] = []
    for s in segmentos:
        if "?" in s:
            vistas += 1
            if vistas > maximo:
                continue  # pregunta de más → se elimina
        fuera.append(s)
    return _rejoin(fuera)


def sanear_texto_libre_haiku(
    texto: str, *, max_preguntas: int = MAX_PREGUNTAS_POR_TURNO
) -> str:
    """Aplica los guards en orden: quita frases prohibidas, recorta preguntas de más
    y limpia marcadores de formato huérfanos. Texto libre de Haiku exclusivamente."""
    paso1 = sanear_frases_prohibidas(texto)
    paso2 = limitar_preguntas(paso1, max_preguntas)
    return limpiar_marcadores_sueltos(paso2)
