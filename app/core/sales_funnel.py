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
_BEATS: dict[str, list[str]] = {
    "1° de Kinder": [
        "el aprendizaje es muy activo y por juego intencional, respetando su etapa",
        "se desarrolla lenguaje, autonomía, motricidad, convivencia y seguridad personal",
        "se nota cuando empieza a seguir rutinas, participar, explorar con confianza y hacer cosas por sí mismo",
        "no trabajamos desde miedo ni presión: un niño seguro sí puede aprender",
    ],
    "2° de Kinder": [
        "ya sostienen mejor las rutinas, participan más y ganan mucha seguridad",
        "el aprendizaje sigue activo y por juego intencional, con más independencia, lenguaje y atención",
        "se nota cuando explica más lo que piensa, participa con intención y necesita menos ayuda",
        "no buscamos que solo respondan correcto, sino que entiendan y se atrevan a pensar",
    ],
    "3° de Kinder": [
        "es el cierre de la etapa y se trabaja mucho la preparación para primaria",
        "se fortalece autonomía, atención, lenguaje, convivencia y seguridad, con más estructura",
        "los papás notan 'ya me explica mejor', 'ya resuelve más solo', 'ya sigue rutinas con seguridad'",
        "primero construimos bases sólidas, antes de pedir rendimiento",
    ],
    "1° de Primaria": [
        "empezamos bases académicas más sólidas, conectadas con comprensión (no solo memorizar)",
        "buscamos que entienda, investigue, participe y explique cómo pensó algo",
        "cuidamos la parte emocional y la autonomía, con aprendizaje activo ligado a situaciones reales",
        "se nota cuando deja de decir 'no sé' y empieza a explicarte cómo resolvió algo",
    ],
    "2° de Primaria": [
        "se consolidan bases académicas más fuertes: leer con más comprensión y escribir con soltura",
        "resuelven explicando los procesos y conectan lo aprendido con situaciones reales",
        "no buscamos repetición, sino comprensión",
        "se nota cuando ya no solo da la respuesta, sino que explica cómo llegó a ella",
    ],
    "3° de Primaria": [
        "se nota mucho más la independencia y el pensamiento crítico",
        "empiezan a argumentar, explicar procesos y tomar iniciativa",
        "hay más profundidad académica, conectada con la vida real",
        "se nota cuando argumenta y toma iniciativa por sí mismo",
    ],
    "1° de Secundaria": [
        "es una etapa más profunda y retadora: se fortalece pensamiento crítico, organización y análisis",
        "se trabaja con proyectos, debate, investigación y análisis de temas reales",
        "se busca capacidad de argumentar, no aprendizaje de memoria",
        "se nota cuando organiza mejor su trabajo y argumenta con criterio",
    ],
    "2° de Secundaria": [
        "se afina mucho la autonomía: gestionar mejor su tiempo, responsabilidades y organización",
        "mejora su forma de trabajar y participar, con más profundidad y análisis",
        "se nota cuando trabaja con más independencia",
    ],
    "3° de Secundaria": [
        "es el cierre de la etapa, con más madurez académica y personal",
        "buscamos más criterio, independencia y capacidad para resolver",
        "más claridad para expresar lo que piensa y seguridad para decidir",
        "se nota cuando resuelve con criterio propio y decide con más seguridad",
    ],
}

# Fallback por NIVEL (grados sin lista, p.ej. maternal).
_BEATS_NIVEL: dict[str, list[str]] = {
    "maternal": [
        "el foco es vínculo, seguridad, exploración y lenguaje",
        "es la base que después le da sentido a lo académico",
        "se nota cuando llega más curioso, más conectado contigo y con palabras nuevas",
    ],
    "kinder": [
        "el aprendizaje es activo y por juego intencional",
        "se nota cuando deja de esperar instrucciones para todo y empieza a proponer",
    ],
    "primaria": [
        "bases académicas sólidas conectadas con comprensión y pensamiento",
        "se nota cuando deja de pedirte la respuesta y empieza a explicarte lo que piensa",
    ],
    "secundaria": [
        "guía emocional, pensamiento crítico y carácter",
        "se nota cuando sostiene una opinión propia y argumenta con criterio",
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
