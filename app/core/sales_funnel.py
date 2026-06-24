"""Flujo de venta de 3 etapas — el CÓDIGO decide la etapa, las transiciones y el
MOMENTO del empuje (regla del contador). Haiku solo redacta el contenido que el
código le inyecta como hint.

Etapa 1 (Enganche): el papá da el nivel → confirma + diferenciador (modelo BEAR),
  NUNCA precio.
Etapa 2 (Valor + empuje): una escena observable del nivel; cuando turnos_valor llega
  al umbral, el código ordena PROPONER la visita asumiendo el siguiente paso.
Etapa 3 (Cierre): conecta al agendado existente (no se reimplementa aquí).

El contenido (BEAR + escenas por nivel) es el MISMO de prompts/journey/educacion.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.campus_resolver import (
    _infer_grado_kinder,
    _infer_grado_primaria,
    _infer_grado_secundaria,
)
from app.core.state import EstadoCapturado

_DISPLAY_NIVEL_GRADO = {"kinder": "Kinder", "primaria": "Primaria", "secundaria": "Secundaria"}


def _grado_de_edad(nivel: str | None, edad: int | None) -> str | None:
    """Infiere el grado canónico ('2° de Kinder') desde la EDAD para kinder/primaria/
    secundaria. None si no aplica. El papá responde con la edad ('tiene 4 años') en vez
    del grado → inferimos el grado y NO nos quedamos en loop pidiéndolo (bug real)."""
    inferidor = {
        "kinder": _infer_grado_kinder,
        "primaria": _infer_grado_primaria,
        "secundaria": _infer_grado_secundaria,
    }.get(nivel or "")
    if inferidor is None or edad is None:
        return None
    g = inferidor(edad=edad, grado_texto=None)
    if g is None:
        return None
    return f"{g}° de {_DISPLAY_NIVEL_GRADO[nivel]}"


def _grado_canonico(nivel: str | None, grado_texto: str | None) -> str | None:
    """Normaliza un grado libre ('cuarto', '4to', '4°', 'cuarto grado') a la forma de la
    KB ('4° de Primaria') usando el nivel. Si el extractor guardó el grado en bruto, esto
    hace que el contenido específico matchee igual. None si no parsea."""
    inferidor = {
        "kinder": _infer_grado_kinder,
        "primaria": _infer_grado_primaria,
        "secundaria": _infer_grado_secundaria,
    }.get(nivel or "")
    if inferidor is None or not grado_texto:
        return None
    g = inferidor(edad=None, grado_texto=grado_texto)
    if g is None:
        return None
    return f"{g}° de {_DISPLAY_NIVEL_GRADO[nivel]}"

# Base de conocimiento oficial — fuente del contenido EXACTO por grado/nivel.
_KB_PATH = Path(__file__).resolve().parent.parent / "kb" / "sofia_kb_oficial.md"


_MODALIDAD_NORM = {
    "cubs": "cubs", "babies": "baby", "baby": "baby",
    "infants": "infants", "toddlers": "toddlers",
}


@lru_cache(maxsize=1)
def _kb_contenido() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Parsea `sofia_kb_oficial.md` → (por_grado, por_nivel, por_modalidad) con el texto
    VERBATIM del documento. Esto permite INYECTAR el contenido exacto del grado/modalidad
    (no pedirle a Haiku que lo busque en todo el documento, donde mezclaba lo general con
    lo específico)."""
    por_grado: dict[str, str] = {}
    por_nivel: dict[str, str] = {}
    por_modalidad: dict[str, str] = {}
    if not _KB_PATH.exists():
        return por_grado, por_nivel, por_modalidad
    text = _KB_PATH.read_text(encoding="utf-8")
    # DETALLE POR GRADO: "**1° de Kinder.** <texto>" hasta el próximo **/##.
    for m in re.finditer(
        r"\*\*(\d°\s+de\s+\w+)\.\*\*\s*(.+?)(?=\n\s*\*\*|\n#{2,}|\Z)", text, re.DOTALL
    ):
        por_grado[m.group(1).strip().lower()] = " ".join(m.group(2).split())
    # MODALIDADES de maternal: "**Babies (12 a 18 meses).** <texto>".
    for m in re.finditer(
        r"\*\*(Cubs|Babies|Baby|Infants|Toddlers)\s*\([^)]*\)\.\*\*\s*(.+?)"
        r"(?=\n\s*\*\*|\n#{2,}|\Z)",
        text,
        re.DOTALL,
    ):
        key = _MODALIDAD_NORM.get(m.group(1).strip().lower())
        if key:
            por_modalidad[key] = " ".join(m.group(2).split())
    # DETALLE POR NIVEL: "**Maternal:** <texto>".
    for m in re.finditer(
        r"\*\*(Maternal|Kinder|Primaria|Secundaria):\*\*\s*(.+?)(?=\n\s*\*\*|\n#{2,}|\Z)",
        text,
        re.DOTALL,
    ):
        por_nivel[m.group(1).strip().lower()] = " ".join(m.group(2).split())
    return por_grado, por_nivel, por_modalidad


def _modalidad_de_edad(
    edad_anos: int | None, edad_meses: int | None = None
) -> str | None:
    """Mapea la edad a la modalidad de maternal. Usa los MESES si los hay (preciso:
    distingue Infants 18-24m de Baby 12-18m); si solo hay años, aproxima. None si no
    hay edad."""
    if edad_meses is not None:
        if edad_meses < 12:
            return "cubs"
        if edad_meses < 18:
            return "baby"
        if edad_meses < 24:
            return "infants"
        return "toddlers"
    if edad_anos is None:
        return None
    if edad_anos <= 0:
        return "cubs"
    if edad_anos == 1:
        return "baby"  # 12-23m sin meses exactos → baby (infants necesita los meses)
    return "toddlers"


# Pregunta del grado/edad cuando el papá da SOLO el nivel (Gaby/Ceci): el contenido
# debe ser específico del grado, no genérico del nivel.
def _hint_pregunta_grado(nivel: str) -> str:
    display = _DISPLAY.get(nivel, nivel)
    if nivel == "maternal":
        pedir = (
            "qué EDAD tiene su bebé (en maternal el grupo y lo que se trabaja cambian "
            "mucho según la edad: Cubs 3-11 meses, Baby 12-18, Infants 18m-2 años, "
            "Toddlers 2 años en adelante)"
        )
    else:
        rango = {
            "kinder": "1°, 2° o 3°",
            "primaria": "1° a 6°",
            "secundaria": "1°, 2° o 3°",
        }.get(nivel, "1°, 2° o 3°")
        pedir = f"en qué GRADO va su hijo ({rango} de {display})"
    return (
        f"[ENGANCHE {display} — el papá dio el nivel pero NO el grado/edad. NO des "
        f"todavía el contenido del nivel (sería genérico). Abre con UNA frase cálida y "
        f"breve sobre {display} en Maple y la filosofía general del colegio (sin nombrar "
        f"'BEAR'), y luego pregunta {pedir} — porque el contenido es muy distinto por "
        f"grado y queremos contarle EXACTAMENTE lo de su etapa. 2-3 frases. La pregunta "
        f"de cierre la pone el sistema; tú NO repreguntes.{_TONO}]"
    )


def _cta_pregunta_grado(nivel: str) -> str:
    if nivel == "maternal":
        return "¿Qué edad tiene tu bebé? Así te cuento justo lo de su etapa 😊"
    display = _DISPLAY.get(nivel, nivel)
    return f"¿En qué grado va tu hijo? Así te cuento cómo se vive justo en ese año de {display} 😊"


# Continuación del papá (sin pregunta nueva) — el contador incrementa con estos.
# (El caller ya descartó preguntas de info nueva antes de llegar aquí.)
STAGE_ENGANCHE = "enganche"
STAGE_VALOR = "valor"
STAGE_CIERRE = "cierre"
STAGE_AGENDADA = "agendada"
STAGE_PIDE_GRADO = "pide_grado"  # se pidió el grado/edad; se espera la respuesta

# Diferenciador oficial (modelo BEAR) — de educacion.md. NO nombres "BEAR" al papá
# salvo que lo pregunte; descríbelo.
_DIFERENCIADOR = (
    "el modelo de Maple no le agrega más cosas al niño, ordena lo que importa en el "
    "orden en que el cerebro se desarrolla — primero seguridad y vínculo, luego "
    "autonomía, después pensamiento profundo, al final propósito. Aquí tu hijo no "
    "solo aprende: se forma."
)

_DISPLAY = {
    "maternal": "Maternal",
    "kinder": "Kinder",
    "primaria": "Primaria",
    "secundaria": "Secundaria",
}

# Contenido POR GRADO como BEATS cortos (de la KB / documents_maple — base de Ceci).
# Cada turno de contenido inyecta 1-2 beats NO USADOS (rastreados en estado) → mensajes
# cortos y sin repetir ideas. El diferenciador va SIEMPRE en el enganche (aparte, nunca
# se "agota"). Grados sin lista caen a _BEATS_NIVEL.
# IMPORTANTE: cada beat de un grado es una FACETA DISTINTA (académico / autonomía /
# socioemocional / un ejemplo concreto / lo observable en casa). Así, escoja la rotación
# 1 o 2 que escoja, el mensaje se siente FRESCO y nunca repite la misma idea entre turnos.
# Beats en PROSA conversacional, SIN el patrón 'Etiqueta: lista' (los dos puntos hacían
# que Haiku escupiera 'La parte emocional:' / 'El liderazgo:', prohibido por Gaby pto 3).
_BEATS: dict[str, list[str]] = {
    "1° de Kinder": [
        "el aprendizaje entra por juego intencional, cantando, manipulando y explorando, sin presión",
        "empieza a hacer cosas por sí mismo, como guardar lo suyo, lavarse las manos y pedir lo que necesita",
        "cuidamos que se sienta seguro y acompañado cuando se separa de ti",
        "un día combina rincones, movimiento, cuento y trabajo en grupos pequeños",
        "se nota cuando llega contándote algo que descubrió y quiere repetirlo en casa",
    ],
    "2° de Kinder": [
        "afianza el lenguaje y ya arma frases más largas para explicarte lo que piensa",
        "sostiene rutinas y normas con menos recordatorios, mucho más independiente",
        "convive mejor, espera turnos, comparte y resuelve roces hablando en vez de con golpes",
        "el aprendizaje sigue por juego, ahora con retos más largos y atención sostenida",
        "se nota cuando deja de pedir ayuda para todo y empieza a proponer sus propias ideas",
    ],
    "3° de Kinder": [
        "es el puente a primaria, donde consolidamos lectura inicial, números y trazo, sin acelerar",
        "madura su autonomía, termina lo que empieza, organiza sus cosas y se concentra más tiempo",
        "fortalece la seguridad para hablar en grupo y sostener lo que piensa",
        "un día mezcla trabajo en mesa, juego con intención y momentos de exploración",
        "los papás notan 'ya me explica mejor' y 'ya resuelve más solo'",
    ],
    "1° de Primaria": [
        "asentamos bases reales, leyendo con comprensión y operando con entendimiento, no de memoria",
        "buscamos que investigue y te explique cómo pensó algo",
        "cuidamos la transición emocional para que el salto a primaria no lo viva con miedo",
        "el trabajo se conecta con su vida, midiendo, comparando y contando cosas reales",
        "se nota cuando deja de decir 'no sé' y empieza a explicarte cómo resolvió algo",
    ],
    "2° de Primaria": [
        "consolidamos lectura con más comprensión y escritura con soltura",
        "resuelve explicando el proceso, no solo dando el resultado",
        "gana autonomía en su trabajo y ya organiza, revisa y corrige lo suyo",
        "conecta lo aprendido con proyectos y situaciones reales",
        "se nota cuando ya no solo da la respuesta, sino que explica cómo llegó a ella",
    ],
    "3° de Primaria": [
        "gana profundidad académica con textos más largos y problemas de varios pasos",
        "despega el pensamiento crítico y empieza a comparar, cuestionar y proponer",
        "toma iniciativa y se hace cargo de sus responsabilidades sin que se lo recuerden",
        "trabaja proyectos donde investiga un tema y lo presenta al grupo",
        "se nota cuando defiende una idea con razones y decide por sí mismo",
    ],
    "1° de Secundaria": [
        "el salto es hacia el pensamiento crítico, donde aprende a analizar, cuestionar fuentes y formar su propia opinión",
        "trabaja por proyectos, investigando un tema real, desarrollándolo y defendiéndolo ante el grupo",
        "afianza su organización y autonomía, gestionando sus tiempos, entregas y materiales solo",
        "acompañamos la parte emocional de la edad, su identidad, sus vínculos y cómo maneja la frustración",
        "se abren espacios para que ejerza liderazgo, coordine equipos, exponga y tome la iniciativa",
    ],
    "2° de Secundaria": [
        "profundiza el análisis, relaciona temas y sostiene su postura con datos",
        "afina su autonomía y organiza su tiempo y responsabilidades con poca supervisión",
        "madura en lo emocional, con más conciencia de sí mismo, de sus relaciones y de sus decisiones",
        "los proyectos suben de nivel, con más investigación, trabajo en equipo y exposición",
        "se nota cuando trabaja con independencia y se hace responsable de sus resultados",
    ],
    "3° de Secundaria": [
        "es el cierre de etapa, con madurez para textos, análisis y proyectos complejos",
        "consolida una autonomía total, planea, ejecuta y rinde cuentas de su trabajo",
        "trabaja su madurez emocional y vocación, con más claridad sobre quién es y hacia dónde va",
        "desarrolla liderazgo y voz propia para exponer y coordinar a otros",
        "se nota cuando decide con seguridad y expresa con claridad lo que piensa",
    ],
}

# Fallback por NIVEL (grados sin lista, p.ej. maternal). También facetas distintas para
# no repetir entre sí ni con los beats por grado.
_BEATS_NIVEL: dict[str, list[str]] = {
    "maternal": [
        "el foco es el vínculo, la seguridad y la confianza, que son la base de todo lo que viene después",
        "estimulamos lenguaje, movimiento y exploración con todos sus sentidos",
        "se nota cuando llega más curioso, más conectado contigo y con palabras nuevas",
    ],
    "kinder": [
        "el aprendizaje entra por juego intencional, respetando su etapa",
        "crece en autonomía, lenguaje y convivencia, sin presión ni miedo",
        "se nota cuando deja de esperar instrucciones para todo y empieza a proponer",
    ],
    "primaria": [
        "bases académicas sólidas conectadas con comprensión, no con memoria",
        "crecen el pensamiento, la autonomía y el trabajo con situaciones reales",
        "se nota cuando deja de pedirte la respuesta y empieza a explicarte lo que piensa",
    ],
    "secundaria": [
        "pensamiento crítico, proyectos y madurez personal en una etapa retadora",
        "acompañamos lo emocional y el carácter, no solo lo académico",
        "se nota cuando sostiene una opinión propia y se hace cargo de lo suyo",
    ],
}

# Kinder: jamás 'proyectos/PBL/Challenge Based Learning'.
_REGLA_KINDER = (
    " En Kinder NUNCA digas 'proyectos', 'PBL' ni 'Challenge Based Learning' — usa "
    "'aprendizaje activo' / 'juego intencional'."
)
# Maternal: la KB exige orientar por edad a la modalidad correcta (NO lista fría).
_REGLA_MATERNAL = (
    " REGLA MATERNAL (de la KB): orienta por EDAD a la modalidad que le toca — "
    "Cubs (3-11 meses), Baby (12-18 meses), Infants (18 meses-2 años), Toddlers "
    "(2 años en adelante). Si ya sabes la edad del bebé, nómbrale SU modalidad con su "
    "rango ('para 1 año es nuestro grupo Baby, de 12 a 18 meses'); si NO sabes la edad, "
    "menciona que hay modalidades por edad y pregúntale la edad del bebé. NUNCA vuelques "
    "las 4 como lista fría."
)


def _maternal_regla(nivel: str) -> str:
    return _REGLA_MATERNAL if nivel == "maternal" else ""


# El CÓDIGO cierra cada etapa con su pregunta (CTA). Haiku NO pregunta nada → así el
# empuje es determinístico y no se cuela el descubrimiento. PERO tiene LIBERTAD para
# redactar cálido y natural sobre los puntos de la base (no recitar, no omitir).
_TONO = (
    " No abras con 'Claro' ni 'Perfecto', no nombres 'BEAR' ni etiquetas tipo "
    "'Concepto: descripción'. MÁXIMO 2-4 frases, cálidas y naturales — breve. SIN "
    "NINGUNA pregunta (el sistema agrega la de cierre). NO pidas edad/grado."
    # 'alto nivel académico' es término general, NO contenido del grado. La KB: "no lo
    # coloques como bandera de venta". En grados chicos suena a exigencia/presión.
    " NO uses la frase 'alto nivel académico' (ni 'nivel académico') en este contenido "
    "de valor — enfócate en lo concreto del grado (comprensión, autonomía, juego, escena "
    "observable). Esa frase solo va si el papá pregunta explícito por el nivel académico."
)


def _kinder_regla(nivel: str) -> str:
    return _REGLA_KINDER if nivel == "kinder" else ""


def _display_grado(nivel: str, grado: str | None) -> str:
    """'2° de Kinder' si hay grado canónico; si no, el nivel ('Kinder')."""
    if grado:
        return grado
    return _DISPLAY.get(nivel, "ese nivel")


def _beats_de(grado: str | None, nivel: str) -> list[str]:
    return _BEATS.get(grado or "") or _BEATS_NIVEL.get(nivel) or []


def _elegir_beats(grado: str | None, nivel: str, usados: list[str], n: int) -> list[str]:
    """Hasta `n` beats NO usados del grado/nivel (en orden). [] si se agotaron."""
    libres = [b for b in _beats_de(grado, nivel) if b not in (usados or [])]
    return libres[: max(0, n)]


def construir_contenido_grado(
    nivel: str,
    grado: str | None,
    usados: list[str],
    *,
    n: int = 2,
    incluir_diferenciador: bool = False,
    edad: int | None = None,
    edad_meses: int | None = None,
) -> tuple[str | None, list[str]]:
    """CLAUDE-CONDUCE (funnel ← KB): el funnel decide CUÁNDO dar valor y de QUÉ
    nivel/grado; el CONTENIDO se inyecta TEXTUAL desde la BASE DE CONOCIMIENTO OFICIAL.
    Una sola fuente de verdad: si Lili/Gaby actualizan la KB, el contenido se actualiza
    solo. `edad` (años) se usa en maternal para inyectar la MODALIDAD que toca.
    Devuelve (hint, []).
    """
    por_grado, por_nivel, por_modalidad = _kb_contenido()
    # Maternal: el texto específico es el de la MODALIDAD según la edad.
    modalidad = _modalidad_de_edad(edad, edad_meses) if nivel == "maternal" else None
    if modalidad and por_modalidad.get(modalidad):
        texto = por_modalidad[modalidad]
        display = {"cubs": "Cubs", "baby": "Baby", "infants": "Infants",
                   "toddlers": "Toddlers"}.get(modalidad, "Maternal")
    else:
        display = _display_grado(nivel, grado)
        texto = por_grado.get((grado or "").lower()) or por_nivel.get(nivel.lower(), "")

    instr_dif = (
        f" Antes del contenido del grado, abre con el diferenciador GENERAL de Maple "
        f"(sin nombrar 'BEAR'), presentado CLARAMENTE como la filosofía del COLEGIO (no "
        f"como algo de este grado): {_DIFERENCIADOR}"
        if incluir_diferenciador
        else ""
    )

    if texto:
        # INYECTAMOS el contenido exacto del grado y prohibimos mezclar lo general.
        contenido = (
            f" Este es el contenido OFICIAL y EXACTO de {display} (del documento). "
            f"Descríbelo SOLO a partir de esto, sin agregar características GENERALES del "
            f'colegio (como "alto nivel académico", el modelo educativo, valores '
            f"generales) — eso NO es específico de {display} y mezclarlo es un error "
            f'grave en educación. Contenido de {display}:\n"{texto}"'
        )
    else:
        contenido = (
            f" Toma el contenido de la sección de {display} en la base de conocimiento; "
            f"NO agregues características generales del colegio que no sean de este grado."
        )

    hint = (
        f"[CONTENIDO {display} — el papá quiere saber de {display}.{contenido} "
        f"Redáctalo cálido y BREVE (2-4 frases), con UNA escena observable (lo que el "
        f"papá vería en su hijo), con tus palabras pero SIN salirte de ese contenido ni "
        f"inventar.{instr_dif} Toca un aspecto DISTINTO a lo ya dicho — no repitas. Sin "
        f"precios.{_kinder_regla(nivel)}{_maternal_regla(nivel)}{_TONO}]"
    )
    return hint, []


def _cta_etapa1(nivel: str, grado: str | None = None) -> str:
    return f"¿Te cuento cómo se ve un día en {_display_grado(nivel, grado)}? 😊"


def hint_contenido(
    nivel: str, grado: str | None, usados: list[str], *, n: int = 2
) -> tuple[str | None, list[str]]:
    """Pausa de contenido del grado → 1-2 beats NO usados (sin diferenciador, ya se dio
    en el enganche). Devuelve (hint, beats_usados)."""
    return construir_contenido_grado(nivel, grado, usados, n=n, incluir_diferenciador=False)


# Faceta (etiqueta legible) por palabra clave — para el RECAP cuando ya no quedan beats
# nuevos: nombra lo YA visto sin re-explicarlo. Orden = orden natural de lectura.
_FACETAS_RECAP: list[tuple[str, tuple[str, ...]]] = [
    (
        "lo académico",
        (
            "académic",
            "lectura",
            "leer",
            "ley",
            "número",
            "operar",
            "operando",
            "texto",
            "comprensión",
            "trazo",
            "escritura",
        ),
    ),
    (
        "el pensamiento crítico",
        (
            "pensamiento crítico",
            "analiz",
            "análisis",
            "cuestiona",
            "compar",
        ),
    ),
    ("los proyectos", ("proyecto", "investig", "defend", "presenta al grupo", "expon")),
    (
        "la autonomía",
        (
            "autonomía",
            "organiz",
            "responsabilidad",
            "independ",
            "su tiempo",
            "por sí mismo",
            "iniciativa",
        ),
    ),
    (
        "lo emocional",
        (
            "emocional",
            "vínculo",
            "frustración",
            "seguro",
            "seguridad",
            "identidad",
            "acompañ",
            "confianza",
        ),
    ),
    ("la convivencia", ("convive", "comparte", "turnos", "roces")),
    ("el liderazgo", ("liderazgo", "coordin", "equipos")),
    ("el juego y la exploración", ("juego intencional", "explor", "rincones")),
]


def recap_beats_vistos(usados: list[str], *, maximo: int = 4) -> str | None:
    """Cuando se AGOTAN los beats: nombra (sin re-explicar) las FACETAS ya vistas, para
    RECONOCER la pregunta del papá en vez de saltar directo a la re-oferta. Devuelve algo
    como 'Ya te conté lo académico, lo emocional y el liderazgo'. None si no reconoce
    ninguna faceta (el caller cae a la re-oferta simple)."""
    if not usados:
        return None
    texto = " ".join(usados).lower()
    vistas: list[str] = []
    for etiqueta, claves in _FACETAS_RECAP:
        if etiqueta not in vistas and any(k in texto for k in claves):
            vistas.append(etiqueta)
    if not vistas:
        return None
    vistas = vistas[:maximo]
    cuerpo = vistas[0] if len(vistas) == 1 else ", ".join(vistas[:-1]) + f" y {vistas[-1]}"
    return f"Ya te conté {cuerpo}"


def _cta_etapa2(empuje: bool) -> str:
    if empuje:
        # Explícito: una VISITA al colegio para conocerlo en persona (no ambiguo).
        return (
            "Lo mejor es que lo conozcas en persona: te invito a una visita al colegio "
            "para que lo veas, sientas el ambiente y resuelvas tus dudas. ¿Te acomoda "
            "esta semana o la siguiente?"
        )
    return "¿Quieres que te cuente algo más de cómo trabajamos?"


@dataclass
class FunnelDecision:
    """Resultado de la máquina de venta para este turno."""

    hint: str | None  # instrucción+contenido para Haiku (None = el funnel no actúa)
    cta: str | None  # pregunta de cierre EMITIDA POR CÓDIGO (se anexa a la respuesta)
    entrar_agendado: bool  # el papá aceptó el empuje → pasar a Etapa 3 (agendado)
    stage: str  # nuevo stage_venta a persistir
    turnos_valor: int  # nuevo contador a persistir
    empuje: bool  # se inyectó la instrucción de empuje este turno
    beats_usados: list[str] | None = None  # beats consumidos (a marcar en estado)
    pedir_grado: bool = False  # se pidió el grado/edad → el orchestrator arma la captura
    pedir_grado_nivel: str | None = None  # nivel del que se pidió el grado


def decidir_funnel(
    capt: EstadoCapturado,
    *,
    es_continuacion: bool,
    nivel_en_msg: str | None,
    pide_info_nueva: bool,
    en_agendado: bool,
    umbral: int,
    beats_usados: list[str] | None = None,
) -> FunnelDecision:
    """Decide la etapa + el contador para este turno.

    - `es_continuacion`: el papá NO trae pregunta nueva (responde "sí/ajá/ok").
    - `nivel_en_msg`: nivel mencionado en el mensaje ('kinder'…) o None.
    - `pide_info_nueva`: el papá pregunta algo concreto → PAUSA el contador.
    - `beats_usados`: ideas ya dichas en la sesión (no repetir).
    """
    stage = capt.stage_venta or STAGE_ENGANCHE
    tv = capt.turnos_valor
    usados = beats_usados if beats_usados is not None else []

    # Cita ya agendada o en pleno agendado → funnel apagado (anti-insistencia).
    if capt.cita_agendada:
        return FunnelDecision(None, None, False, STAGE_AGENDADA, tv, False)
    if en_agendado:
        return FunnelDecision(None, None, False, STAGE_CIERRE, tv, False)

    # Grado canónico capturado ("2° de Kinder") → contenido específico de ese grado.
    h = capt.hijo_efectivo()
    grado = h.grado if (h and h.grado) else None
    edad = h.edad if h else None
    edad_meses = h.edad_meses if h else None
    nivel_ctx = nivel_en_msg or (
        capt.nivel_buscado_actual.value if capt.nivel_buscado_actual else None
    )

    # Normaliza el grado libre del extractor ("cuarto"/"4to") a la forma de la KB
    # ("4° de Primaria") para que el contenido ESPECÍFICO matchee y no caiga a genérico.
    if grado and nivel_ctx:
        grado = _grado_canonico(nivel_ctx, grado) or grado
    # Si el papá respondió con la EDAD (no el grado) para K/P/S, inferimos el grado de la
    # edad (4 años → 2° de Kinder, 7 → 2° de Primaria) en vez de seguir pidiéndolo en loop.
    if grado is None and edad is not None:
        grado = _grado_de_edad(nivel_ctx, edad)

    def _especifico(nv: str | None) -> bool:
        """¿Tenemos la unidad ESPECÍFICA? grado (K/P/S) o edad/modalidad (maternal)."""
        if nv == "maternal":
            return edad is not None or edad_meses is not None
        return grado is not None

    # Pregunta de info nueva → PAUSA: ni incrementa ni empuja ni inyecta hint.
    if pide_info_nueva:
        return FunnelDecision(None, None, False, stage, tv, False)

    # El papá da el nivel.
    if nivel_en_msg is not None:
        # GABY/CECI: si dio el nivel pero NO el grado/edad, PRIMERO se pide — así el
        # contenido siempre es específico del grado, no genérico del nivel.
        if not _especifico(nivel_en_msg):
            return FunnelDecision(
                _hint_pregunta_grado(nivel_en_msg),
                _cta_pregunta_grado(nivel_en_msg),
                False, STAGE_PIDE_GRADO, 0, False,
                pedir_grado=True, pedir_grado_nivel=nivel_en_msg,
            )
        # Ya hay grado/edad → contenido ESPECÍFICO (Etapa 1).
        hint, beats = construir_contenido_grado(
            nivel_en_msg, grado, usados, n=1, incluir_diferenciador=True,
            edad=edad, edad_meses=edad_meses
        )
        return FunnelDecision(
            hint, _cta_etapa1(nivel_en_msg, grado),
            False, STAGE_VALOR, 1, False, beats_usados=beats,
        )

    # GRADO/EDAD SUELTO sin nombrar el nivel ("cuarto grado") pero el estado YA resolvió
    # nivel+grado (el extractor lo dedujo) → damos el contenido ESPECÍFICO igual, en lugar
    # de quedarnos en "Perfecto, cuarto grado de primaria." sin nada (bug real de Gaby).
    if stage == STAGE_ENGANCHE and nivel_ctx and _especifico(nivel_ctx):
        hint, beats = construir_contenido_grado(
            nivel_ctx, grado, usados, n=1, incluir_diferenciador=True,
            edad=edad, edad_meses=edad_meses,
        )
        return FunnelDecision(
            hint, _cta_etapa1(nivel_ctx, grado),
            False, STAGE_VALOR, 1, False, beats_usados=beats,
        )

    # El papá ACABA de dar el grado/edad tras pedírselo → contenido ESPECÍFICO de una vez
    # (SIN diferenciador: ya se dio la frase general al pedir el grado).
    if stage == STAGE_PIDE_GRADO:
        nivel = capt.nivel_buscado_actual.value if capt.nivel_buscado_actual else None
        if nivel and _especifico(nivel):
            hint, beats = construir_contenido_grado(
                nivel, grado, usados, n=1, incluir_diferenciador=False,
                edad=edad, edad_meses=edad_meses
            )
            return FunnelDecision(
                hint, _cta_etapa1(nivel, grado),
                False, STAGE_VALOR, 1, False, beats_usados=beats,
            )
        # Aún no se captó el grado/edad → re-pedir con suavidad (sin avanzar el contador).
        if nivel:
            return FunnelDecision(
                _hint_pregunta_grado(nivel), _cta_pregunta_grado(nivel),
                False, STAGE_PIDE_GRADO, 0, False,
                pedir_grado=True, pedir_grado_nivel=nivel,
            )

    # Continuación dentro del funnel (ya en 'valor').
    if stage == STAGE_VALOR and es_continuacion:
        # Si ya se ofreció el empuje (tv >= umbral) y el papá CONTINÚA → acepta → cierre.
        if tv >= umbral:
            return FunnelDecision(None, None, True, STAGE_CIERRE, tv, False)
        nivel = capt.nivel_buscado_actual.value if capt.nivel_buscado_actual else None
        if nivel is None:
            return FunnelDecision(None, None, False, stage, tv, False)
        nuevo_tv = tv + 1
        empuje = nuevo_tv >= umbral
        # Etapa 2: contenido específico del grado/modalidad.
        hint, beats = construir_contenido_grado(nivel, grado, usados, n=2, edad=edad, edad_meses=edad_meses)
        return FunnelDecision(
            hint,
            _cta_etapa2(empuje),
            False,
            STAGE_VALOR,
            nuevo_tv,
            empuje,
            beats_usados=beats,
        )

    # Nada que hacer (el caller deja que Haiku/otra rama responda).
    return FunnelDecision(None, None, False, stage, tv, False)
