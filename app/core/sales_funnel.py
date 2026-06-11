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

# Esencia por nivel (Etapa 1) — condensada de educacion.md.
_ESENCIA = {
    "maternal": (
        "En maternal el foco es vínculo, seguridad, exploración y lenguaje — la base "
        "que después le da sentido a lo académico."
    ),
    "kinder": (
        "En kinder se construye el amor por aprender, con aprendizaje activo y juego "
        "intencional."
    ),
    "primaria": (
        "En primaria se combinan bases académicas sólidas con pensamiento crítico y "
        "proyectos reales."
    ),
    "secundaria": (
        "En secundaria el foco es que el adolescente se conozca: guía emocional, "
        "pensamiento crítico, debate y carácter."
    ),
}

# Escena observable por nivel (Etapa 2) — copiadas de educacion.md ("lo notas en casa…").
_ESCENA = {
    "maternal": (
        "Lo notas en casa: tu hijo llega más curioso, más conectado contigo, con "
        "palabras nuevas que él mismo busca usar."
    ),
    "kinder": (
        "Lo notas en casa cuando tu hijo deja de esperar instrucciones para todo y "
        "empieza a proponer — te dice 'mira lo que hice' antes de que le preguntes."
    ),
    "primaria": (
        "Lo notas en casa cuando deja de pedirte la respuesta y empieza a explicarte "
        "lo que él piensa, con sus propias palabras."
    ),
    "secundaria": (
        "Lo notas cuando sostiene una opinión propia sin agresión y sin necesitar la "
        "aprobación de todos — te plantea un argumento que no habías considerado."
    ),
}

# Kinder: jamás 'proyectos/PBL/Challenge Based Learning'.
_REGLA_KINDER = (
    " En Kinder NUNCA digas 'proyectos', 'PBL' ni 'Challenge Based Learning' — usa "
    "'aprendizaje activo' / 'juego intencional'."
)
# El CÓDIGO cierra cada etapa con su pregunta (CTA). Haiku NO pregunta nada → así el
# empuje es determinístico y no se cuela el descubrimiento viejo (edad/"¿qué te importa?").
_TONO = (
    " No abras con 'Claro' ni 'Perfecto', no nombres 'BEAR'. Escribe SOLO el valor "
    "(1-3 frases cálidas), SIN NINGUNA pregunta — el sistema agrega la pregunta de "
    "cierre. NO pidas la edad ni el grado, NO preguntes '¿qué es lo que te importa?'."
)


def _kinder_regla(nivel: str) -> str:
    return _REGLA_KINDER if nivel == "kinder" else ""


def _hint_etapa1(nivel: str) -> str:
    display = _DISPLAY.get(nivel, "ese nivel")
    return (
        f"[ETAPA VENTA — ENGANCHE. El papá busca {display}. Confírmalo en tono cálido "
        f"SIN dar ningún precio. Transmite breve el diferenciador: {_DIFERENCIADOR} "
        f"{_ESENCIA.get(nivel, '')} PROHIBIDO mencionar precios, costos o inscripción."
        f"{_kinder_regla(nivel)}{_TONO}]"
    )


def _hint_etapa2(nivel: str, empuje: bool) -> str:
    display = _DISPLAY.get(nivel, "ese nivel")
    return (
        f"[ETAPA VENTA — VALOR ({display}). Comparte UNA escena observable, cálida y "
        f"concreta: {_ESCENA.get(nivel, '')} Sin precios.{_kinder_regla(nivel)}{_TONO}]"
    )


def _cta_etapa1(nivel: str) -> str:
    return f"¿Te cuento cómo se ve un día en {_DISPLAY.get(nivel, 'ese nivel')}? 😊"


def _cta_etapa2(empuje: bool) -> str:
    if empuje:
        return "Lo mejor es vivirlo en persona. ¿Te acomoda esta semana o la siguiente?"
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


def decidir_funnel(
    capt: EstadoCapturado,
    *,
    es_continuacion: bool,
    nivel_en_msg: str | None,
    pide_info_nueva: bool,
    en_agendado: bool,
    umbral: int,
) -> FunnelDecision:
    """Decide la etapa + el contador para este turno.

    - `es_continuacion`: el papá NO trae pregunta nueva (responde "sí/ajá/ok").
    - `nivel_en_msg`: nivel mencionado en el mensaje ('kinder'…) o None.
    - `pide_info_nueva`: el papá pregunta algo concreto → PAUSA el contador.
    """
    stage = capt.stage_venta or STAGE_ENGANCHE
    tv = capt.turnos_valor

    # Cita ya agendada o en pleno agendado → funnel apagado (anti-insistencia).
    if capt.cita_agendada:
        return FunnelDecision(None, None, False, STAGE_AGENDADA, tv, False)
    if en_agendado:
        return FunnelDecision(None, None, False, STAGE_CIERRE, tv, False)

    # Pregunta de info nueva → PAUSA: ni incrementa ni empuja ni inyecta hint.
    if pide_info_nueva:
        return FunnelDecision(None, None, False, stage, tv, False)

    # El papá da el nivel → Etapa 1 (diferenciador, sin precio). Arranca el contador.
    if nivel_en_msg is not None:
        return FunnelDecision(
            _hint_etapa1(nivel_en_msg), _cta_etapa1(nivel_en_msg),
            False, STAGE_VALOR, 1, False,
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
        return FunnelDecision(
            _hint_etapa2(nivel, empuje), _cta_etapa2(empuje),
            False, STAGE_VALOR, nuevo_tv, empuje,
        )

    # Nada que hacer (el caller deja que Haiku/otra rama responda).
    return FunnelDecision(None, None, False, stage, tv, False)
