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

from dataclasses import dataclass

from app.core.state import EstadoCapturado

# Continuación del papá (sin pregunta nueva) — el contador incrementa con estos.
# (El caller ya descartó preguntas de info nueva antes de llegar aquí.)
STAGE_ENGANCHE = "enganche"
STAGE_VALOR = "valor"
STAGE_CIERRE = "cierre"
STAGE_AGENDADA = "agendada"

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
_BEATS: dict[str, list[str]] = {
    "1° de Kinder": [
        "el aprendizaje entra por juego intencional: cantar, manipular y explorar, sin presión",
        "gana autonomía en su día: guardar sus cosas, lavarse las manos, pedir lo que necesita",
        "cuidamos lo emocional: que se sienta seguro, visto y acompañado al separarse de ti",
        "un día combina rincones, movimiento, cuento y trabajo en grupos pequeños",
        "se nota cuando llega contándote algo que descubrió y quiere repetirlo en casa",
    ],
    "2° de Kinder": [
        "afianza el lenguaje: arma frases más largas y te explica lo que piensa",
        "sostiene rutinas y normas con menos recordatorios, mucho más independiente",
        "convive mejor: espera turnos, comparte y resuelve roces con palabras, no con golpes",
        "el aprendizaje sigue por juego, ahora con retos más largos y atención sostenida",
        "se nota cuando deja de pedir ayuda para todo y empieza a proponer sus propias ideas",
    ],
    "3° de Kinder": [
        "es el puente a primaria: consolidamos lectura inicial, números y trazo, sin acelerar",
        "madura su autonomía: termina lo que empieza, organiza sus cosas y se concentra más tiempo",
        "fortalece la seguridad para hablar en grupo y sostener lo que piensa",
        "un día mezcla trabajo en mesa, juego con intención y momentos de exploración",
        "los papás notan 'ya me explica mejor' y 'ya resuelve más solo'",
    ],
    "1° de Primaria": [
        "asentamos bases reales: leer comprendiendo y operar entendiendo el porqué, no de memoria",
        "buscamos que investigue y te explique cómo pensó algo",
        "cuidamos la transición emocional: que el salto a primaria no lo viva con miedo",
        "el trabajo se conecta con su vida: medir, comparar y contar cosas reales",
        "se nota cuando deja de decir 'no sé' y empieza a explicarte cómo resolvió algo",
    ],
    "2° de Primaria": [
        "consolidamos lectura con más comprensión y escritura con soltura",
        "resuelve explicando el proceso, no solo dando el resultado",
        "gana autonomía en su trabajo: organiza, revisa y corrige lo suyo",
        "conecta lo aprendido con proyectos y situaciones reales",
        "se nota cuando ya no solo da la respuesta, sino que explica cómo llegó a ella",
    ],
    "3° de Primaria": [
        "más profundidad académica: textos más largos y problemas de varios pasos",
        "despega el pensamiento crítico: compara, cuestiona y propone",
        "toma iniciativa y se hace cargo de sus responsabilidades sin que se lo recuerden",
        "trabaja proyectos donde investiga un tema y lo presenta al grupo",
        "se nota cuando defiende una idea con razones y decide por sí mismo",
    ],
    "1° de Secundaria": [
        "el salto es a pensamiento crítico: analizar, cuestionar fuentes y formar opinión propia",
        "trabaja por proyectos: investiga un tema real, lo desarrolla y lo defiende ante el grupo",
        "afianza organización y autonomía: gestiona sus tiempos, entregas y materiales solo",
        "acompañamos la parte emocional de la edad: identidad, vínculos y manejar la frustración",
        "se abren espacios de liderazgo: coordinar equipos, exponer y tomar la iniciativa",
    ],
    "2° de Secundaria": [
        "profundiza el análisis: relaciona temas y sostiene su postura con datos",
        "afina la autonomía: organiza su tiempo y responsabilidades con poca supervisión",
        "madura en lo emocional: más conciencia de sí mismo, sus relaciones y sus decisiones",
        "los proyectos suben de nivel: más investigación, trabajo en equipo y exposición",
        "se nota cuando trabaja con independencia y se hace responsable de sus resultados",
    ],
    "3° de Secundaria": [
        "es el cierre de etapa: madurez para textos, análisis y proyectos complejos",
        "consolida autonomía total: planea, ejecuta y rinde cuentas de su trabajo",
        "trabaja madurez emocional y vocación: claridad sobre quién es y hacia dónde va",
        "desarrolla liderazgo y voz propia para exponer y coordinar a otros",
        "se nota cuando decide con seguridad y expresa con claridad lo que piensa",
    ],
}

# Fallback por NIVEL (grados sin lista, p.ej. maternal). También facetas distintas para
# no repetir entre sí ni con los beats por grado.
_BEATS_NIVEL: dict[str, list[str]] = {
    "maternal": [
        "el foco es vínculo, seguridad y confianza: la base de todo lo que viene después",
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
# El CÓDIGO cierra cada etapa con su pregunta (CTA). Haiku NO pregunta nada → así el
# empuje es determinístico y no se cuela el descubrimiento. PERO tiene LIBERTAD para
# redactar cálido y natural sobre los puntos de la base (no recitar, no omitir).
_TONO = (
    " No abras con 'Claro' ni 'Perfecto', no nombres 'BEAR' ni etiquetas tipo "
    "'Concepto: descripción'. MÁXIMO 2-4 frases, cálidas y naturales — breve. SIN "
    "NINGUNA pregunta (el sistema agrega la de cierre). NO pidas edad/grado."
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
) -> tuple[str | None, list[str]]:
    """Devuelve (hint, beats_usados): hint con el diferenciador (si aplica, SIEMPRE) +
    1-2 beats NO usados; Haiku lo redacta breve. (None, []) si no quedan beats ni hay
    diferenciador (caller reduce con gracia)."""
    beats = _elegir_beats(grado, nivel, usados, n)
    partes: list[str] = []
    if incluir_diferenciador:
        partes.append(_DIFERENCIADOR)  # el diferenciador SIEMPRE va en el enganche
    partes.extend(beats)
    if not partes:
        return None, []
    display = _display_grado(nivel, grado)
    hint = (
        f"[CONTENIDO {display} — el papá quiere saber de {display}. Redáctalo cálido y "
        f"BREVE (máx 1-2 ideas, 2-4 frases) con TUS palabras: {' '.join(partes)} Sin "
        f"precios.{_kinder_regla(nivel)}{_TONO}]"
    )
    return hint, beats


def _cta_etapa1(nivel: str, grado: str | None = None) -> str:
    return f"¿Te cuento cómo se ve un día en {_display_grado(nivel, grado)}? 😊"


def hint_contenido(
    nivel: str, grado: str | None, usados: list[str], *, n: int = 2
) -> tuple[str | None, list[str]]:
    """Pausa de contenido del grado → 1-2 beats NO usados (sin diferenciador, ya se dio
    en el enganche). Devuelve (hint, beats_usados)."""
    return construir_contenido_grado(nivel, grado, usados, n=n, incluir_diferenciador=False)


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

    # Pregunta de info nueva → PAUSA: ni incrementa ni empuja ni inyecta hint.
    if pide_info_nueva:
        return FunnelDecision(None, None, False, stage, tv, False)

    # El papá da el nivel → Etapa 1 (diferenciador SIEMPRE + 1 beat). Arranca el contador.
    if nivel_en_msg is not None:
        hint, beats = construir_contenido_grado(
            nivel_en_msg, grado, usados, n=1, incluir_diferenciador=True
        )
        return FunnelDecision(
            hint, _cta_etapa1(nivel_en_msg, grado),
            False, STAGE_VALOR, 1, False, beats_usados=beats,
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
        # Etapa 2: 1-2 beats NO usados. Si se agotaron → hint None → solo la CTA (gracia).
        hint, beats = construir_contenido_grado(nivel, grado, usados, n=2)
        return FunnelDecision(
            hint, _cta_etapa2(empuje), False, STAGE_VALOR, nuevo_tv, empuje,
            beats_usados=beats,
        )

    # Nada que hacer (el caller deja que Haiku/otra rama responda).
    return FunnelDecision(None, None, False, stage, tv, False)
