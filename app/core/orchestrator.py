"""Orchestrator — procesa un turno de conversación.

Flujo:
1. Cargar/crear EstadoConversacion.
2. Comandos especiales (Modo Aprendizaje maple2026 / /salir).
3. Extraer estado del mensaje (state_extractor) + clasificar intención
   (intent_classifier) en paralelo.
4. Mapear intención → posible cambio de fase del journey.
5. Componer system prompt (prompt_builder con caching).
6. Llamar a Claude Haiku 4.5 con memoria reciente.
7. **Validators determinísticos** — si fallan, regenerar (max N veces).
8. Registrar frases munición usadas (anti-repetición futura).
9. Persistir: mensajes (user + assistant), turn_log, estado actualizado.

NO incluye tools custom (Bloque 4).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.appointment_extractor import (
    TZ_MONTERREY,
    contiene_expresion_temporal,
    mensaje_resuelve_fecha,
    mensaje_resuelve_hora,
)
from app.core.appointment_flow import (
    AppointmentHandlerResult,
    handle_appointment_intent,
)
from app.core.appointment_messages import render_registration_message
from app.core.intent_classifier import (
    Intent,
    IntentResult,
    classify_intent,
    es_respuesta_corta_al_turno_previo,
    menciona_info_exploratoria,
    quiere_agendar_explicito,
    quiere_persona_humana,
)
from app.core.learning_mode import guardar_feedback
from app.core.oferta_resolver import (
    detectar_consulta_oferta,
    extraer_figuras,
    extraer_grado_suelto,
    horario_subnivel_de_estado,
    nivel_buscado_de_mensaje,
    precio_nivel_de_estado,
    sanear_cifras_ajenas,
)
from app.core.output_guards import (
    recortar_oraciones,
    sanear_sondeo,
    sanear_texto_libre_haiku,
)
from app.core.prompt_builder import build_system_blocks
from app.core.repository import get_repository
from app.core.sales_funnel import decidir_funnel, hint_contenido, recap_beats_vistos
from app.core.state import (
    Canal,
    EstadoConversacion,
    FaseAgendado,
    FaseJourney,
    Modo,
)
from app.core.state_extractor import aplicar_extraccion, extraer_de_mensaje
from app.core.validators import (
    ValidationReport,
    extraer_frases_municion_usadas,
    run_all_validators,
)
from app.observability.costs import calculate_cost
from app.tools.campus import get_campus_para_nivel
from app.tools.estancias import get_estancias, render_estancias_bloque
from app.tools.horarios import get_horario
from app.tools.niveles import consultar_edades_de_nivel
from app.tools.precios import get_precio, get_todos_precios

log = logging.getLogger(__name__)

# FLUJO DE VENTA — intents que piden un DATO específico (costos/horario/estancia/becas/
# campus/prepa/proceso). Estos PAUSAN el contador del funnel (se responde el dato y NO
# se empuja). Preguntar por el NIVEL o la METODOLOGÍA es parte del valor (el
# diferenciador lo responde) → NO pausa.
_DATA_INTENTS = frozenset(
    {
        Intent.PREGUNTA_COSTOS,
        Intent.PREGUNTA_HORARIO,
        Intent.PREGUNTA_ESTANCIAS,
        Intent.PREGUNTA_BECAS,
        Intent.PREGUNTA_CAMPUS,
        Intent.PREGUNTA_PREPA,
        Intent.PREGUNTA_PROCESO_ADMISION,
    }
)

# Intents donde el papá hace una pregunta SUSTANTIVA: durante la colección del
# agendado, Haiku SÍ responde estos (tiene la info/tools). En cualquier otro intent
# (dar datos, respuesta corta, confuso), la pregunta del campo la pone el código.
_PREGUNTAS_SUSTANTIVAS = frozenset(
    {
        Intent.PREGUNTA_COSTOS,
        Intent.PREGUNTA_HORARIO,
        Intent.PREGUNTA_NIVEL,
        Intent.PREGUNTA_METODOLOGIA,
        Intent.PREGUNTA_PROCESO_ADMISION,
        Intent.PREGUNTA_ESTANCIAS,
        Intent.PREGUNTA_BECAS,
        Intent.PREGUNTA_CAMPUS,
        Intent.PREGUNTA_PREPA,
        Intent.PREGUNTA_GENERAL_MAPLE,
        Intent.MENCIONA_DIAGNOSTICO,
        Intent.OBJECION_CARO,
        Intent.OBJECION_FLEXIBLE,
        Intent.OBJECION_TAREA,
        Intent.OBJECION_OTRA,
    }
)

# Comandos especiales — Modo Aprendizaje
COMANDO_ENTRAR_APRENDIZAJE = "maple2026"
COMANDOS_SALIR_APRENDIZAJE = ("/salir", "salir")

MENSAJE_MODO_APRENDIZAJE_ACTIVADO = (
    "🔧 Modo Aprendizaje activado.\n"
    "Hola equipo. Estoy lista para recibir su feedback. Pueden decirme:\n"
    "- Qué respondí mal y cómo debí responder\n"
    "- Información nueva que debo aprender\n"
    "- Reglas o prohibiciones que debo agregar\n"
    "- Ajustes a mi tono o comportamiento\n\n"
    "Escucho y tomo nota."
)

MENSAJE_MODO_NORMAL_ACTIVADO = (
    "🟢 Modo Normal activado. Volví a mi rol de admisiones. Lista para atender prospectos."
)


@dataclass
class TurnResult:
    """Resultado de procesar un turno."""

    response: str
    session_id: str
    fase_journey: FaseJourney
    intent: Intent | None = None
    cost_usd: Decimal = Decimal("0")
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached: int = 0
    latency_ms: int = 0
    model_used: str = ""
    turn_number: int = 0
    skip_persistencia: bool = False  # para mensajes de sistema (modo aprendizaje)
    metadata: dict[str, Any] = field(default_factory=dict)
    validators_failed: list[str] = field(default_factory=list)
    validators_warnings: list[str] = field(default_factory=list)
    regenerations: int = 0


# ¿El mensaje es una PREGUNTA de contenido? (interrogativo o frase de "qué hacen/se
# fortalece/cómo es"). Sirve para que una pregunta tras el empuje NO se tome como
# aceptación de la visita.
_INTERROGATIVO_RE = re.compile(
    r"\?"
    r"|^\s*(?:qu[eé]|c[oó]mo|cu[aá]l(?:es)?|cu[aá]nt\w*|cu[aá]ndo|d[oó]nde|"
    r"por\s+qu[eé]|para\s+qu[eé])\b"
    r"|\bse\s+fortalece\b|\bc[oó]mo\s+(?:es|son|funciona|trabajan)\b"
    r"|\bqu[eé]\s+(?:hacen|trabajan|ven|aprenden|pasa|incluye|tal)\b"
    r"|\by\s+(?:el|la|los|las|de|en)\b",
    re.IGNORECASE,
)


def _es_interrogativo(mensaje: str) -> bool:
    return bool(_INTERROGATIVO_RE.search((mensaje or "").strip()))


_NIVELES_KW_RE = re.compile(
    r"\b(maternal|kinder|primaria|secundaria|prepa|preparatoria)\b", re.IGNORECASE
)


def _menciona_multiples_niveles(mensaje: str, capt: Any) -> bool:
    """True si el papá menciona 2+ niveles DISTINTOS EN ESTE MENSAJE → señal de 'dos
    hijos en niveles distintos': el funnel se hace a un lado y Haiku corre el protocolo
    de la KB (uno a la vez, pregunta con cuál empezar).

    Se evalúa SOLO sobre el mensaje (no sobre el estado): así, cuando el papá YA eligió
    con cuál empezar ('empecemos por kinder'), el mensaje trae un solo nivel y el funnel
    arranca normal — no se queda 'pegado' por tener 2 hijos guardados."""
    m = mensaje or ""
    encontrados = {x.lower() for x in _NIVELES_KW_RE.findall(m)}
    if {"prepa", "preparatoria"} & encontrados:
        encontrados -= {"prepa", "preparatoria"}
        encontrados.add("prepa")
    if len(encontrados) >= 2:
        return True
    # DOS HIJOS por edad: "dos niños", o "uno de 4 y otra de 9" (dos edades con otro/otra).
    ml = m.lower()
    if re.search(r"\bdos\s+(?:hijos?|ni[ñn]os?|peques?|nen[eo]s?|chamacos?|ni[ñn]as?)\b", ml):
        return True
    if "otro" in ml or "otra" in ml:
        edades_ctx = [int(e) for e in re.findall(r"\bde\s+(\d{1,2})\b", ml)]
        if len({e for e in edades_ctx if 0 < e <= 17}) >= 2:
            return True
    return False


_DEFER_LILI = "Ese dato te lo confirma Miss Lili en la cita 😊"

# ANTI-LOOP: cuando el papá re-pregunta algo que no pudimos detallar, NO repetir el mismo
# bloque (queja #1 de los papás simulados) → ESCALAR con Lily / ofrecer la visita.
_ESCALACION_LOOP = (
    "Veo que esto te importa y no quiero darte vueltas 🙏 Para darte el dato exacto y que "
    "resuelvas todo al instante, lo mejor es que te conecte con Lily, de nuestro equipo de "
    "admisiones. ¿Me compartes tu nombre y tu WhatsApp para que te contacte hoy mismo? O si "
    "prefieres, agendamos una visita y ahí te explican cada detalle. 😊"
)


# El papá pide el DESGLOSE / total / cuotas extra → dar el detalle completo (no evadir).
_GASTOS_DESGLOSE_RE = re.compile(
    r"\bgastos?\s+iniciales?\b|\bcuotas?\s+(?:extra|adicional|inicial|otra)|"
    r"\bqu[ée]\s+m[áa]s\s+se\s+paga\b|\bdesglose\b|\bel\s+total\b|"
    r"\bcu[áa]nto\s+(?:es\s+|sale\s+|ser[íi]a\s+)?(?:en\s+)?total\b|\bcu[áa]nto\s+sale\s+todo\b|"
    r"\bcon\s+todo\s+incluido\b|\btodo\s+junto\b|\bel\s+seguro\b|\blos\s+seguros\b|"
    r"\botras?\s+cuotas?\b|\bqu[ée]\s+otras?\s+(?:cuotas|cosas|cobros)\b|\bcobros?\s+(?:extra|sorpresa)",
    re.IGNORECASE,
)


def _nucleo_respuesta(t: str) -> str:
    """Quita el prefijo 'Como te comentaba,' para comparar el CONTENIDO real entre turnos
    (el loop alternaba con/sin ese prefijo, así que sin esto no se detectaba)."""
    return re.sub(r"^\s*como te comentaba,?\s*", "", (t or "").strip(), flags=re.IGNORECASE).strip()

# Preguntas que NO son de horario escolar aunque el clasificador LLM las marque así:
#  - "¿cómo es un día?", "el día a día", "qué hacen en un día" → CONTENIDO de la etapa
#    (de hecho el funnel cierra con "¿te cuento cómo se ve un día en X?").
#  - "¿cuántas horas de inglés/francés?" → currículo, no el horario de entrada/salida.
# Sin esto, todas caían en el callejón "¿de qué nivel necesitas el horario?".
_DIA_CONTENIDO_RE = re.compile(
    r"\b(?:un|el|su)\s+d[íi]a\b|"  # "cómo es un día", "un día como es", "el día a día"
    r"\bd[íi]a\s+a\s+d[íi]a\b|\bla\s+jornada\b|\bqu[ée]\s+hacen?\b|"
    r"\bhoras?\b[^.?!\n]{0,18}\b(?:ingl[ée]s|franc[ée]s)\b|"  # "horas (son) de inglés"
    r"\b(?:ingl[ée]s|franc[ée]s)\b[^.?!\n]{0,18}\bhoras?\b|"  # "inglés … horas"
    r"\bcu[áa]nto\s+(?:ingl[ée]s|franc[ée]s)\b|\bqu[ée]\s+tanto\s+ingl[ée]s\b|"
    r"\bcu[áa]ntas?\s+horas?\s+de\s+(?:clase|materia|deporte|arte|m[úu]sica)\b",
    re.IGNORECASE,
)
# …salvo que SÍ pregunten explícitamente por el horario escolar (entrada/salida).
_HORARIO_EXPLICITO_RE = re.compile(
    r"\ba\s+qu[ée]\s+hora\b|\bhora\s+de\s+(?:entrada|salida)\b|\bqu[ée]\s+horario\b|"
    r"\bhorario\s+de\b|\bhora\s+(?:entran?|salen?)\b",
    re.IGNORECASE,
)
# Términos PROPIOS de Maple (marcas/programas). El clasificador LLM no los reconoce y los
# manda a 'confuso' → menú genérico, en vez de explicarlos. Los reencaminamos a pregunta
# general para que Haiku responda con el contenido OFICIAL de la KB (aporte de Gaby).
_TERMINOS_MAPLE_RE = re.compile(
    r"\bkonnect\b|\bhigh\s*scope\b|\bglobal\s*breakers?\b|\bsing\s*it\b|"
    r"\bchallenge\s*week\b|\blego\b|\blabor\s+social\b|\bdisciplina\s+positiva\b",
    re.IGNORECASE,
)
# Materias especiales / programas / idiomas que van INCLUIDOS en el programa académico
# (KB: "incluidas en el horario escolar / programa académico"). Si preguntan si TIENEN
# COSTO EXTRA, la respuesta es que están incluidos — NO la colegiatura. (NO incluye las
# academias extracurriculares de la tarde ni estancias, que sí son servicio adicional.)
_MATERIA_INCLUIDA_RE = re.compile(
    r"\brob[óo]tica\b|\blego\b|\bprogramaci[óo]n\b|\bfranc[ée]s\b|\bm[úu]sica\b|"
    r"\beducaci[óo]n\s+f[íi]sica\b|\bkonnect\b|\bglobal\s*breakers?\b|\bsing\s*it\b|"
    r"\bchallenge\s*week\b|\blabor\s+social\b|\bmaterias?\s+especiales?\b",
    re.IGNORECASE,
)
# Grado 4°/5°/6° SUELTO ("cuarto grado", "5to", "sexto") = inequívocamente PRIMARIA (esos
# grados solo existen en primaria). Respaldo si el papá responde "¿qué nivel?" con el grado
# y no nombra "primaria" (bug real de Gaby: "Cuarto grado" → no daba contenido).
# OJO: el símbolo "°"/"º" no lleva \b al final (no es char de palabra) → "4°" no matcheaba.
_GRADO_ALTO_PRIMARIA_RE = re.compile(
    r"\b(?:cuarto|quinto|sexto)\b|\b[456]\s*(?:to|vo|mo)\b|\b[456]\s*[°º]|\b[456]\s*grado\b",
    re.IGNORECASE,
)
# "tiene 9 años" / "9 años" → edad en años (para mapear edad→nivel+grado como primer dato).
_EDAD_ANOS_RE = re.compile(r"\b(\d{1,2})\s*a[ñn]os?\b", re.IGNORECASE)
_DISPLAY_NIVEL_ORCH = {"kinder": "Kinder", "primaria": "Primaria", "secundaria": "Secundaria"}
# Ordinal BAJO suelto ("segundo", "tercero", "2°") SIN nivel → ambiguo (K/P/S) → pedir nivel.
_ORDINAL_BAJO_AMBIGUO_RE = re.compile(
    r"^\s*(?:primer[oa]?|segund[oa]|tercer[oa]?|[123]\s*[°º]?)\s*(?:grado)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# ANTI-INVENTO: temas que NO están en la KB. Sofía inventaba (psicopedagogía, comedor,
# # alumnos por salón, examen de admisión) en vez de diferir. Respuesta determinística que
# NO inventa y lleva a la visita. (alberca/transporte ya difieren bien; no se incluyen.)
_NL_PSICO_RE = re.compile(r"\bpsic[óo]log|\bpsicopedag|\bterapeut|\bterapias?\b", re.IGNORECASE)
_NL_COMEDOR_RE = re.compile(r"\bcomedor\b|\bqu[ée]\s+(?:les\s+)?dan\s+de\s+comer\b", re.IGNORECASE)
_NL_CUPO_RE = re.compile(
    r"\bcu[áa]ntos?\s+(?:alumnos|ni[ñn]os|estudiantes|beb[ée]s)\b|"
    r"\b(?:alumnos|ni[ñn]os|estudiantes|beb[ée]s)\s+por\s+(?:sal[óo]n|grupo|maestra?)\b|"
    r"\bcupos?\b|\bcu[áa]ntos?\s+por\s+(?:grupo|sal[óo]n|maestra?)\b|\bratio\b|"
    r"\bproporci[óo]n\b|\bmaestras?\s+por\b",
    re.IGNORECASE,
)
# Datos puntuales que Maple NO tiene digitalizados (instalaciones específicas, lista de
# útiles/uniformes, etc.) → diferir con HONESTIDAD y ofrecer conseguirlo, NO ir a horario
# ni repetir en loop (queja real: "canchas?" → daba el horario 3 veces).
_NL_NODATO_RE = re.compile(
    r"\bcanchas?\b|\balberca\b|\bgimnasio\b|\binstalaciones\b|\bsal[óo]n\s+de\b|"
    r"\buniformes?\b|\b[úu]tiles\b|\blista\s+de\s+[úu]tiles\b|\bexcursion|\bmateriales?\b|"
    r"\beventos?\b|\bcanch",
    re.IGNORECASE,
)
_NL_EXAMEN_RE = re.compile(
    r"\bexamen\s+de\s+(?:admisi|ingreso)|\bprueba\s+de\s+(?:admisi|ingreso)|\bhacen?\s+examen\b",
    re.IGNORECASE,
)
# Certificado oficial de inglés → NO emiten Cambridge/TOEFL (debe decirlo claro, no desviar).
_NL_CERTIF_ING_RE = re.compile(
    r"\bcertificad[oa]s?\b.*\b(?:ingl[ée]s|idioma)\b|\b(?:cambridge|toefl|ielts)\b|"
    r"\bingl[ée]s\b.*\bcertificad",
    re.IGNORECASE,
)
# Ubicación/dirección → respuesta determinística con la dirección COMPLETA (un guard la
# cortaba en "Col." porque el punto de la abreviatura parece fin de oración).
_UBICACION_RE = re.compile(
    r"\bd[óo]nde\s+(?:est[áa]n|queda|se\s+ubica|los\s+encuentro)|\bubicaci[óo]n\b|"
    r"\bdirecci[óo]n\b|\bc[óo]mo\s+llego\b|\ben\s+qu[ée]\s+(?:zona|parte|colonia)\b|\bsucursal",
    re.IGNORECASE,
)
# Preguntas de ADAPTACIÓN/personalidad (tímido, inquieto…) → seguridad emocional, NO precios.
_ADAPTA_RE = re.compile(
    r"\bt[íi]mid|\binquiet|\bse\s+adapt|\badaptar[íi]a|\bsocializ|\bberrinch|\bnervios|"
    r"\bllora\s+mucho|\ble\s+cuesta\s+(?:socializar|relacionar|hacer\s+amigos)",
    re.IGNORECASE,
)


def _grado_de_edad_texto(e: int) -> str | None:
    if 3 <= e <= 5:
        return f"{e - 2}° de Kinder"
    if 6 <= e <= 11:
        return f"{e - 5}° de Primaria"
    if 12 <= e <= 14:
        return f"{e - 11}° de Secundaria"
    if e <= 2:
        return "Maternal"
    return None


def _respuesta_dos_hijos(mensaje: str) -> str | None:
    """DOS HIJOS por edad → respuesta determinística que nombra el grado de cada uno y
    pregunta con cuál empezar (antes: a veces menú genérico, a veces respuesta muerta)."""
    ml = (mensaje or "").lower()
    es_dos = bool(
        re.search(r"\bdos\s+(?:hijos?|ni[ñn]os?|ni[ñn]as?|peques?|nen[eo]s?)\b", ml)
    ) or bool(re.search(r"\buno\s+de\s+\d+.*?(?:otr[oa]|uno)\s+de\s+\d", ml))
    if not es_dos:
        return None
    pares = re.findall(r"(\d{1,2})\s*a[ñn]os|\bde\s+(\d{1,2})\b", ml)
    edades = [int(a or b) for a, b in pares if (a or b) and 0 < int(a or b) <= 17]
    if len(edades) < 2:
        return None
    g1, g2 = _grado_de_edad_texto(edades[0]), _grado_de_edad_texto(edades[1])
    if not g1 or not g2:
        return None
    return (
        f"¡Qué bien, dos! 😊 Por sus edades, el de {edades[0]} estaría en {g1} y el de "
        f"{edades[1]} en {g2}. ¿Con cuál te gustaría que empecemos para contarte a detalle?"
    )


def _respuesta_especial(mensaje: str) -> str | None:
    """Respuesta determinística (code-only, sin Haiku) para temas que NO están en la KB
    (evita invención) o para preguntas de adaptación (evita el menú de precios)."""
    m = mensaje or ""
    _dh = _respuesta_dos_hijos(m)
    if _dh:
        return _dh
    if _UBICACION_RE.search(m):
        return (
            "Estamos en Saltillo, Coahuila, con dos campus 📍\n"
            "• Campus 1: José Figueroa Siller 156, Col. Doctores — Maternal, Kinder y "
            "Primaria (hasta 5°)\n"
            "• Campus 2: Blvd. V. Carranza 5064, Col. Doctores — 6° de Primaria a Secundaria\n"
            "Si te queda algo retirado, no te preocupes — muchas familias vienen de distintas "
            "zonas. ¿Te gustaría agendar una visita para conocernos? 😊"
        )
    if _NL_PSICO_RE.search(m):
        return (
            "Maple es una escuela inclusiva y acompaña a cada niño según lo que necesita 💛 "
            "El detalle de cómo se da ese acompañamiento te lo explican mejor en la visita. "
            "¿Te gustaría agendar para conocerlo?"
        )
    if _NL_COMEDOR_RE.search(m):
        return (
            "En Maple los desayunos y snacks están incluidos 😊 El detalle del menú te lo "
            "muestran cuando vengas a conocernos. ¿Te gustaría agendar una visita?"
        )
    if _NL_CUPO_RE.search(m):
        return (
            "Trabajamos con grupos pequeños para que cada niño reciba atención personalizada 😊 "
            "El número exacto por grupo y la disponibilidad te los confirman en la visita. "
            "¿Te gustaría agendar?"
        )
    if _NL_EXAMEN_RE.search(m):
        return (
            "El proceso de admisión te lo explica nuestro equipo en la cita de informes 😊 "
            "¿Te gustaría que agendemos para que te cuenten los detalles?"
        )
    if _NL_NODATO_RE.search(m):
        return (
            "Buena pregunta 😊 Ese detalle puntual no lo tengo a la mano aquí, pero te lo "
            "consigo con el equipo y te lo paso sin problema. ¿Me compartes tu WhatsApp para "
            "mandártelo? También lo ves directo cuando vengas a conocer el colegio."
        )
    if _NL_CERTIF_ING_RE.search(m):
        return (
            "No emitimos certificados oficiales tipo Cambridge o TOEFL. Lo que sí "
            "construimos es un nivel real de inglés (entre B2 y C1 al terminar la "
            "trayectoria) que le permite presentar esos exámenes por su cuenta con "
            "seguridad. ¿Te cuento cómo trabajamos el inglés por etapas?"
        )
    if _ADAPTA_RE.search(m):
        return (
            "Entiendo tu inquietud 💛 En Maple lo primero que cuidamos es que tu hijo se sienta "
            "seguro y acompañado — esa es la base de todo. Cada niño tiene su ritmo y aquí lo "
            "respetamos, sin presión; lo notas cuando llega más tranquilo y confiado. "
            "¿Te gustaría conocer cómo lo acompañamos, en una visita?"
        )
    return None


# Saludo formal de presentación ("¡Hola! …Soy Sofía, del equipo de admisiones…"). Va SOLO
# en el primer turno; si Haiku lo repite a media conversación (bug visto), se recorta.
_SALUDO_REPETIDO_RE = re.compile(
    r"^\s*¡?\s*hola[^.!?¡]*[.!?]\s*(?:qu[ée]\s+gusto[^.!?¡]*[.!?]\s*)?"
    r"soy\s+sof[íi]a[^.!?]*?admisiones[^.!?]*[.!?]\s*",
    re.IGNORECASE,
)

# Línea fija de cierre para turnos de info (code-emitida, corta y cálida). Una sola
# pregunta, transaccional (no sondeo).
_CIERRE_INFO = "¿Quieres saber algo más o agendamos una visita? 😊"

# Explicación FIJA de la cita de informes, code-emitida en el PRIMER turno del
# agendado (no es sondeo: le decimos al papá qué va a recibir). Va ANTES de la
# pregunta del día.
_EXPLICACION_CITA_INFORMES = (
    "Con gusto. La visita es una cita de informes: una conversación con nuestro "
    "equipo donde te contamos cómo trabajamos, resuelves tus dudas y conoces las "
    "instalaciones (dura ~1 hora)."
)

# Reorientación code-emitida cuando Haiku no tiene una consulta concreta que responder
# (Bloque B-3) — útil y breve, sin sondeo ni monólogo.
_REORIENTA_SALUDO = (
    "¡Hola! 😊 ¿Para qué nivel te interesa información — maternal, kinder, primaria o secundaria?"
)
_REORIENTA_GENERAL = (
    "Con gusto te ayudo 😊 Puedo contarte de los niveles, costos, horarios y "
    "estancias, o agendarte una visita. ¿Qué te gustaría saber?"
)
_REORIENTA_GENERAL_ALT = (
    "Dime qué te interesa — niveles, costos, horarios, estancias o agendar una "
    "visita — y lo vemos enseguida 😊"
)

# Papá pide hablar con una persona / no quiere un "robot": NUNCA revelar que es IA;
# cálido y reconducir, sin menú robótico.
_RESPUESTA_PERSONA = (
    "Soy Sofía, del equipo de admisiones de Maple 😊 Estoy aquí para acompañarte en "
    "todo este proceso. Cuéntame, ¿qué te gustaría saber o para qué nivel buscas?"
)
_RESPUESTA_PERSONA_ALT = (
    "Aquí sigo contigo, soy Sofía de admisiones de Maple 😊 Con gusto te acompaño — "
    "dime en qué te ayudo y lo vemos juntos."
)

# Re-oferta de la visita tras responder contenido en la etapa de VALOR (aún no agendado).
# 'conocer el colegio' (no 'conocerlo', que era ambiguo: ¿el niño o el colegio?).
_REOFERTA_VALOR = (
    "¿Te animas a conocer el colegio en una visita? Puede ser esta semana o la siguiente 😊"
)


def _reoferta_visita(capt: Any, now: datetime | None = None) -> str:
    """Re-oferta de continuar la visita tras una pausa de info en el agendado, según el
    paso: si ya hay día → pide la HORA de ese día; si no → re-ofrece las fechas.
    `now` etiqueta hoy/mañana (hora de Saltillo) en el día, igual que la propuesta."""
    from app.core.appointment_extractor import fecha_humana_solo_dia
    from app.core.appointment_messages import formato_opciones_dia, prep_dia

    if getattr(capt, "cita_fecha_slot", None) and not getattr(capt, "cita_hora_slot", None):
        dia = fecha_humana_solo_dia(capt.cita_fecha_slot, now) or "ese día"
        return (
            f"Cuando quieras seguimos con tu visita 😊 ¿A qué hora {prep_dia(dia)} {dia} "
            f"te viene bien?"
        )
    ops = getattr(capt, "opciones_dia_propuestas", None)
    if ops:
        try:
            fechas = [datetime.fromisoformat(o) for o in ops]
            txt = formato_opciones_dia(fechas, now)
            return f"Cuando quieras seguimos con tu visita 😊 ¿Qué día te queda mejor: {txt}?"
        except (ValueError, TypeError):
            pass
    return "Cuando quieras seguimos con tu visita 😊 ¿Qué día te queda mejor?"


def _variar_respuesta(texto: str) -> str:
    """Evita repetir IDÉNTICO el mensaje anterior: usa una variante del bloque fijo
    o antepone un conector natural."""
    alt = {
        _REORIENTA_GENERAL: _REORIENTA_GENERAL_ALT,
        _REORIENTA_GENERAL_ALT: _REORIENTA_GENERAL,
        _RESPUESTA_PERSONA: _RESPUESTA_PERSONA_ALT,
        _RESPUESTA_PERSONA_ALT: _RESPUESTA_PERSONA,
    }.get(texto.strip())
    if alt:
        return alt
    return "Como te comentaba, " + texto.lstrip()


# "¿cuáles son las modalidades?" / "detállame" / "costos" → lista completa.
# "¿tienen estancia?" (sí/no) → confirmar + ofrecer, sin volcar la lista.
_ESTANCIA_LISTA_RE = re.compile(
    r"\b(?:cu[áa]les|qu[ée]\s+(?:modalidad|opcion)|modalidades|opciones|detall|"
    r"cu[áa]nto|costos?|precios?|lista|mu[ée]strame|m[áa]ndame|ver\s+las|todas)\b",
    re.IGNORECASE,
)
_ESTANCIA_CONFIRMA = (
    "🏫 ¡Sí! Tenemos horario extendido de 7:00 a.m. a 7:00 p.m. con varias "
    "modalidades (mañana, media, completa, por día y academia individual). "
    "¿Quieres que te detalle las opciones?"
)


async def _construir_oferta(
    estado: EstadoConversacion, tipos: set[str], mensaje: str = ""
) -> list[str]:
    """Líneas con las cifras EXACTAS de costo/horario/estancia, emitidas por el
    CÓDIGO desde las tablas. Si no se puede resolver el nivel/grado, emite una
    línea que pide el dato o defiere a Miss Lili — NUNCA un número inventado."""
    lineas: list[str] = []

    if "costos" in tipos:
        nivel = precio_nivel_de_estado(estado)
        # ¿pide el DESGLOSE/total/cuotas extra? → damos el detalle completo (no evadir, era
        # el loop #1: "cuánto son las cuotas/el seguro/el total/qué más se paga/con todo").
        pide_desglose = bool(_GASTOS_DESGLOSE_RE.search(mensaje))
        if nivel:
            p = await get_precio(nivel)
            if p:
                lineas.append(p.bloque_gastos_completo() if pide_desglose else f"💰 {p.bloque_costos()}")
            else:
                lineas.append(f"💰 {_DEFER_LILI}")
        else:
            # No se pudo resolver el nivel exacto. NUNCA volcar la tabla cruda con las
            # claves internas de BD ('primaria_baja' $6,100; 'primaria_alta' $6,300) — el
            # papá no debe ver eso (queja real de Gaby). Pedimos el dato en humano:
            #  - Primaria sin grado → el costo difiere por grado → pedir el grado.
            #  - Sin nivel claro (o varios hijos) → pedir el nivel.
            nivel_act = estado.estado_capturado.nivel_buscado_actual
            if nivel_act and nivel_act.value == "primaria":
                estado.estado_capturado.pendiente_grado_costos = True
                lineas.append(
                    "💰 El costo de Primaria depende del grado. ¿En qué grado va tu "
                    "peque (1° a 3° o 4° a 6°)?"
                )
            else:
                lineas.append(
                    "💰 Con gusto te paso el costo. ¿Para qué nivel es? "
                    "1️⃣ Maternal · 2️⃣ Kinder · 3️⃣ Primaria · 4️⃣ Secundaria"
                )

    if "horario" in tipos:
        sub, necesita_grado = horario_subnivel_de_estado(estado)
        if sub:
            h = await get_horario(sub)
            lineas.append(f"🕐 {h.bloque()}" if h else f"🕐 {_DEFER_LILI}")
        elif necesita_grado:
            # Respeta el NIVEL ya guardado (no decir "Kinder" si dijo Primaria) y deja
            # marcado que el SIGUIENTE grado suelto ("3") resuelve el horario.
            estado.estado_capturado.pendiente_grado_horario = True
            nivel_act = estado.estado_capturado.nivel_buscado_actual
            nivel_disp = {
                "kinder": "Kinder",
                "primaria": "Primaria",
            }.get(nivel_act.value if nivel_act else "", "ese nivel")
            ejemplo = (
                "1° a 3° o 4° a 6°"
                if (nivel_act and nivel_act.value == "primaria")
                else "1°, 2° o 3°"
            )
            lineas.append(
                f"🕐 El horario de {nivel_disp} depende del grado. ¿En qué grado va tu "
                f"peque ({ejemplo})?"
            )
        else:
            lineas.append("🕐 ¿De qué nivel/grado necesitas el horario?")

    if "estancias" in tipos:
        # Pregunta sí/no ("¿tienen estancia?") → confirma + ofrece; pedido de lista
        # ("¿cuáles son las modalidades?"/"costos") → vuelca las 5 con sus cifras.
        pide_lista = bool(_ESTANCIA_LISTA_RE.search(mensaje or ""))
        if not pide_lista:
            lineas.append(_ESTANCIA_CONFIRMA)
        else:
            nivel_est = precio_nivel_de_estado(estado)
            estancias = await get_estancias(nivel=nivel_est)
            if estancias:
                lineas.append("🏫 " + render_estancias_bloque(estancias))
            else:
                lineas.append(f"🏫 {_DEFER_LILI}")

    return lineas


async def procesar_turno(
    mensaje: str,
    session_id: str,
    *,
    canal: Canal | None = None,
    tester: bool = False,
    now: datetime | None = None,
) -> TurnResult:
    """Procesa un turno completo de conversación.

    Args:
        mensaje: texto del usuario (ya transcrito si era audio, descrito si era imagen).
        session_id: prefijado por canal ('whatsapp:...'|'telegram:...'|'web:...').
        canal: opcional, se infiere del session_id si no se pasa.
        tester: si True, marca la conversación como prueba interna.

    Returns:
        TurnResult con la respuesta de Sofía y metadata.
    """
    started = time.perf_counter()
    settings = get_settings()
    repo = get_repository()
    # `now` en hora de Saltillo (America/Monterrey, UTC-6) cuando el caller no lo pasa —
    # los webhooks no lo mandan. Así el etiquetado hoy/mañana se calcula bien (no en UTC).
    if now is None:
        now = datetime.now(TZ_MONTERREY)

    # 1. Cargar o crear estado
    estado = await repo.get_conversation(session_id)
    es_nueva = estado is None
    if estado is None:
        estado = EstadoConversacion.nueva(session_id)
        if canal is not None:
            estado.canal = canal
        estado.tester = tester
        await repo.upsert_conversation(estado)

    # 2. Procesar comandos especiales (Modo Aprendizaje) ANTES de llamar LLMs
    msg_lower = mensaje.strip().lower()

    if msg_lower == COMANDO_ENTRAR_APRENDIZAJE and estado.modo == Modo.NORMAL:
        estado.modo = Modo.APRENDIZAJE
        await repo.upsert_conversation(estado)
        await _persist_user_message(repo, estado, mensaje)
        await _persist_assistant_message(repo, estado, MENSAJE_MODO_APRENDIZAJE_ACTIVADO)
        latency = int((time.perf_counter() - started) * 1000)
        return TurnResult(
            response=MENSAJE_MODO_APRENDIZAJE_ACTIVADO,
            session_id=session_id,
            fase_journey=estado.fase_journey,
            latency_ms=latency,
            turn_number=await repo.count_turns(session_id),
            skip_persistencia=True,
        )

    if msg_lower in COMANDOS_SALIR_APRENDIZAJE and estado.modo == Modo.APRENDIZAJE:
        estado.modo = Modo.NORMAL
        await repo.upsert_conversation(estado)
        await _persist_user_message(repo, estado, mensaje)
        await _persist_assistant_message(repo, estado, MENSAJE_MODO_NORMAL_ACTIVADO)
        latency = int((time.perf_counter() - started) * 1000)
        return TurnResult(
            response=MENSAJE_MODO_NORMAL_ACTIVADO,
            session_id=session_id,
            fase_journey=estado.fase_journey,
            latency_ms=latency,
            turn_number=await repo.count_turns(session_id),
            skip_persistencia=True,
        )

    # Si estamos en Modo Aprendizaje, guardar mensaje como feedback pendiente.
    # El LLM aún se llama (con prompt modo_aprendizaje) para generar un acuse
    # estructurado, pero el cambio NO se aplica al prompt — solo se registra.
    if estado.modo == Modo.APRENDIZAJE:
        feedback_id = await guardar_feedback(
            session_id=session_id,
            feedback_text=mensaje,
            contexto_anterior=await _ultimos_dos_turnos_resumen(repo, session_id),
        )
        log.info(
            "modo_aprendizaje feedback registrado",
            extra={"session_id": session_id, "feedback_id": feedback_id},
        )
        # Sigue al flujo normal de LLM (con prompt modo_aprendizaje activo) para
        # que Sofía emita el "📝 REGISTRO DE APRENDIZAJE" estructurado.

    # 3. Cargar historial reciente PRIMERO (lo necesitamos para guard de
    # saludo_inicial y para contexto del classifier). Hotfix post-5.7.
    historial = await repo.list_recent_messages(session_id, limit=20)
    hay_turno_previo_assistant = any(
        (m.get("role") or "").lower() in ("assistant", "ai") for m in historial
    )
    # Último mensaje de Sofía — usado por el rescate por confirmación (FIX (b))
    # y por el contexto de respuesta-corta (7bis).
    ultimo_assistant_msg: str | None = None
    for m in reversed(historial):
        if (m.get("role") or "").lower() in ("assistant", "ai"):
            ultimo_assistant_msg = m.get("content")
            break
    # Últimos 3 mensajes con prefijo de rol → contexto para desambiguar
    # mensajes ambiguos del papá (ej. "interactuara y que aprenda").
    historial_para_classifier: list[str] = []
    for m in historial[-6:]:
        role = (m.get("role") or "").lower()
        role_short = "papá" if role in ("user", "human") else "Sofía"
        content = (m.get("content") or "").strip()[:200]
        if content:
            historial_para_classifier.append(f"{role_short}: {content}")

    # 3b. Extraer estado y clasificar intención en paralelo (auxiliares baratos)
    extraccion_task = asyncio.create_task(
        extraer_de_mensaje(mensaje, estado.estado_capturado, ultimo_assistant=ultimo_assistant_msg)
    )
    intent_task = asyncio.create_task(
        classify_intent(
            mensaje,
            historial_reciente=historial_para_classifier,
            hay_turno_previo_assistant=hay_turno_previo_assistant,
        )
    )
    extraccion, intent_result = await asyncio.gather(extraccion_task, intent_task)

    # Término PROPIO de Maple ("¿qué es Konnect?", "LEGO", "Global Breakers"…) que el
    # clasificador mandó a 'confuso' → reencaminar a pregunta general para que Haiku lo
    # explique desde la KB en vez de soltar el menú genérico.
    if intent_result.intent == Intent.CONFUSO_OTRO and _TERMINOS_MAPLE_RE.search(mensaje):
        intent_result = IntentResult(
            intent=Intent.PREGUNTA_GENERAL_MAPLE,
            confidence=1.0,
            razonamiento_breve="término propio de Maple → general",
        )

    # 4-pre. RE-ARMADO (2026-06-02). Una sesión que YA cerró una cita (CERRADO) y
    # se reusa (en WhatsApp la sesión = el teléfono, persiste para siempre) puede
    # agendar OTRA cita. Antes quedaba clavada en CERRADO → el pipeline no corría y
    # `cita_agendada` viejo desarmaba el validador → ghost-close. Re-armamos SOLO
    # con intent EXPLÍCITO QUIERE_AGENDAR (un temporal suelto NO reabre una cita).
    #
    # FIX (2026-06-02b): el reseteo va ANTES de aplicar_extraccion, para que los
    # datos del MENSAJE DISPARADOR ("se llama Lucía, 5 años, el jueves 11am") se
    # capturen sobre el estado limpio y NO se borren. Así no se re-pregunta lo que
    # ya vino en ese mismo mensaje.
    # FIX (2026-06-02c): el trigger del re-armado NO depende solo del clasificador
    # LLM (que falla "quiero agendar otra" → confuso_otro). Respaldo determinístico
    # con regex. El clasificador no debe ser load-bearing.
    # GUARD (2026-06-10): pedir info (informes/costos/horarios) NO es agendar. El
    # clasificador LLM mete "quiero informes" a QUIERE_AGENDAR (la cita se llama
    # "cita de informes") → solo la señal CLARA de visita/cita (regex determinístico)
    # cuenta siempre; el intent LLM solo si el mensaje NO es una consulta de info.
    pide_info_exploratoria = menciona_info_exploratoria(mensaje)
    quiere_reagendar = quiere_agendar_explicito(mensaje) or (
        intent_result.intent == Intent.QUIERE_AGENDAR and not pide_info_exploratoria
    )
    if estado.estado_capturado.fase_agendado == FaseAgendado.CERRADO and quiere_reagendar:
        prev = estado.estado_capturado
        prev.fase_agendado = FaseAgendado.AGENDANDO
        prev.cita_fecha_slot = None
        prev.cita_hora_slot = None
        prev.cita_agendada = False
        prev.fecha_cita = None
        prev.campus_cita = None
        prev.hijos = []  # la nueva cita suele ser de OTRO hijo; se recaptura limpio
        prev.nivel_buscado_actual = None
        estado.agendado = False
        estado.fecha_agendado = None
        log.info(
            "agendado_fase CERRADO→AGENDANDO (re-armado nueva cita)",
            extra={"session_id": session_id},
        )

    # 4. Aplicar extracción al estado (sobre el estado ya re-armado si aplicó):
    # captura nombre/edad/etc del mensaje disparador del segundo agendado.
    estado.estado_capturado = aplicar_extraccion(estado.estado_capturado, extraccion)

    # 4bis. PASO 1 (2026-05-29) — máquina PEGAJOSA de agendado controlada por
    # CÓDIGO. Se entra a AGENDANDO con la PRIMERA señal (intent QUIERE_AGENDAR o
    # expresión temporal) y NO se reevalúa a la baja: el código colecta los 6 datos
    # + día/hora hasta cerrar. Persiste en sofia_conversations.estado_capturado.
    capt = estado.estado_capturado

    # GRADO SUELTO para HORARIOS: si la rama de horarios pidió el grado, un "3"/"4to"/
    # "1 a 3" lo resuelve → se fija en el hijo y se re-emite el horario (no loop).
    grado_horario_resuelto = False
    if capt.pendiente_grado_horario:
        grado_h = extraer_grado_suelto(mensaje, capt.nivel_buscado_actual)
        if grado_h:
            if capt.hijos:
                capt.hijos[0].grado = grado_h
            else:
                from app.core.state import HijoInfo

                capt.hijos = [HijoInfo(nivel=capt.nivel_buscado_actual, grado=grado_h)]
            capt.pendiente_grado_horario = False
            grado_horario_resuelto = True

    # GRADO SUELTO para COSTOS: la rama de precios pidió el grado de primaria (baja vs
    # alta). Un "3"/"segundo"/"4to" lo resuelve → se fija en el hijo y se re-emite el
    # PRECIO correcto (no la tabla cruda). Igual que el de horario, pero para costos.
    grado_costos_resuelto = False
    if capt.pendiente_grado_costos:
        grado_c = extraer_grado_suelto(mensaje, capt.nivel_buscado_actual)
        if grado_c:
            if capt.hijos:
                capt.hijos[0].grado = grado_c
            else:
                from app.core.state import HijoInfo

                capt.hijos = [HijoInfo(nivel=capt.nivel_buscado_actual, grado=grado_c)]
            capt.pendiente_grado_costos = False
            grado_costos_resuelto = True

    # GRADO PARA EL FUNNEL: si el funnel pidió el grado el turno anterior, este turno un
    # grado SUELTO ("3", "tercero", "1° de primaria") lo captura → contenido específico.
    # (La EDAD de maternal la captura la extracción normal; aquí solo el grado de K/P/S.)
    if capt.pendiente_grado_funnel:
        # Fija el nivel al que el funnel preguntó (inmune a que el extractor LLM lo cambie
        # por el contexto viejo), SALVO que el papá nombre OTRO nivel en este mismo turno.
        if capt.pendiente_grado_nivel and nivel_buscado_de_mensaje(mensaje) is None:
            from app.core.state import NivelEducativo

            try:
                capt.nivel_buscado_actual = NivelEducativo(capt.pendiente_grado_nivel)
            except ValueError:
                pass
        grado_f = extraer_grado_suelto(mensaje, capt.nivel_buscado_actual)
        if grado_f:
            if capt.hijos:
                capt.hijos[0].grado = grado_f
                capt.hijos[0].nivel = capt.nivel_buscado_actual  # alinea nivel↔grado
            else:
                from app.core.state import HijoInfo

                capt.hijos = [HijoInfo(nivel=capt.nivel_buscado_actual, grado=grado_f)]
        capt.pendiente_grado_funnel = False
        capt.pendiente_grado_nivel = None

    # EDAD EN MESES (maternal): captura DETERMINÍSTICA para distinguir la modalidad
    # (Infants 18-24m vs Baby 12-18m). El extractor LLM guarda la edad en AÑOS y pierde
    # los meses → "20 meses" se volvía 1 año → Baby (divergencia). Aquí la rescatamos.
    _m_meses = re.search(r"\b(\d{1,2})\s*mes", mensaje.lower())
    if _m_meses:
        _meses = int(_m_meses.group(1))
        if 1 <= _meses <= 47:
            # Setear en el hijo PERSISTIDO (no en hijo_efectivo(), que es una copia).
            if not capt.hijos:
                from app.core.state import HijoInfo

                capt.hijos = [HijoInfo()]
            capt.hijos[0].edad_meses = _meses

    # FLUJO DE VENTA (3 etapas) — el CÓDIGO decide etapa, contador y MOMENTO del empuje;
    # Haiku solo redacta el hint. pide_info_nueva PAUSA el contador (responde el dato y
    # no empuja); continuación lo incrementa; al umbral se ordena el empuje; si el papá
    # CONTINÚA tras el empuje, acepta → entra al agendado existente.
    nivel_en_msg = nivel_buscado_de_mensaje(mensaje)
    # RESPALDO DETERMINÍSTICO (primer dato): el papá responde "¿qué nivel?" con la EDAD o
    # con un grado SUELTO sin nombrar el nivel. Resolvemos nivel (+grado/edad) para que el
    # funnel dé el CONTENIDO — no vacío, no "Qué bueno", no grado inventado ("13 años→7°
    # grado", "4 años→3° Kinder"). Solo si aún no hay nivel en el mensaje ni en el estado.
    # 4°/5°/6° o cuarto/quinto/sexto = SIEMPRE primaria (inequívoco). Corrige al extractor
    # si adivinó otro nivel ("sexto" → secundaria) — el sync de abajo realinea nivel+grado.
    if nivel_en_msg is None and _GRADO_ALTO_PRIMARIA_RE.search(mensaje):
        from app.core.state import HijoInfo, NivelEducativo

        nivel_en_msg = NivelEducativo.PRIMARIA
        _g_canon = extraer_grado_suelto(mensaje, NivelEducativo.PRIMARIA)
        if not capt.hijos:
            capt.hijos = [HijoInfo()]
        capt.hijos[0].nivel = NivelEducativo.PRIMARIA
        if _g_canon:
            capt.hijos[0].grado = _g_canon
    # Edad / meses como PRIMER dato (solo si aún no hay nivel Y no son DOS hijos) → nivel+grado.
    elif (
        nivel_en_msg is None
        and capt.nivel_buscado_actual is None
        and not _menciona_multiples_niveles(mensaje, capt)
    ):
        from app.core.state import HijoInfo, NivelEducativo

        _niv = _gr = _edad = None
        if _m_meses:  # capturado arriba → maternal (la modalidad la da edad_meses)
            _niv = "maternal"
        elif (_ma := _EDAD_ANOS_RE.search(mensaje)) is not None:
            _edad = int(_ma.group(1))
            if _edad <= 2:
                _niv = "maternal"
            elif 3 <= _edad <= 5:
                _niv, _gr = "kinder", _edad - 2  # 3→1°, 4→2°, 5→3°
            elif 6 <= _edad <= 11:
                _niv, _gr = "primaria", _edad - 5  # 6→1° … 11→6°
            elif 12 <= _edad <= 14:
                _niv, _gr = "secundaria", _edad - 11  # 12→1°, 13→2°, 14→3°
        if _niv:
            nivel_en_msg = NivelEducativo(_niv)
            if not capt.hijos:
                capt.hijos = [HijoInfo()]
            capt.hijos[0].nivel = NivelEducativo(_niv)
            if _edad is not None:
                capt.hijos[0].edad = _edad
            if _gr is not None:
                capt.hijos[0].grado = f"{_gr}° de {_DISPLAY_NIVEL_ORCH[_niv]}"
    # DOS HIJOS / MULTI-NIVEL (protocolo de la KB): si el papá menciona 2+ niveles
    # distintos (o ya hay 2+ hijos con niveles distintos), el funnel se HACE A UN LADO
    # → Haiku corre el protocolo del documento (uno a la vez, pregunta con cuál empezar)
    # en vez de meterse de cabeza a un solo nivel.
    multi_nivel = _menciona_multiples_niveles(mensaje, capt)
    if multi_nivel:
        nivel_en_msg = None
        log.info(
            "multi_nivel → funnel a un lado (protocolo dos hijos)",
            extra={"session_id": session_id},
        )
    pide_info_nueva = (
        intent_result.intent in _DATA_INTENTS
        or bool(detectar_consulta_oferta(mensaje))
        or grado_horario_resuelto  # resolver el grado = turno de info (horario)
        or grado_costos_resuelto  # resolver el grado = turno de info (costos)
    )
    # ¿El mensaje parsea como FECHA/HORA válida? (precedencia: la fecha/hora gana).
    es_fecha_valida = capt.cita_fecha_slot is None and mensaje_resuelve_fecha(
        mensaje, capt.opciones_dia_propuestas, now
    )
    es_hora_valida = (
        capt.cita_fecha_slot is not None
        and capt.cita_hora_slot is None
        and mensaje_resuelve_hora(mensaje, capt.ultimo_campo_pedido)
    )
    # PREGUNTA DE CONTENIDO del grado: interrogativa, hay nivel (msg o estado), NO es
    # fecha/hora, NO es DATA, NO es agendar explícito. Así una pregunta tras el empuje
    # ("que se fortalece?") NO se toma como aceptar la visita.
    es_contenido_pregunta = (
        not es_fecha_valida
        and not es_hora_valida
        and not pide_info_nueva
        and not quiere_agendar_explicito(mensaje)
        and (nivel_en_msg is not None or capt.nivel_buscado_actual is not None)
        and _es_interrogativo(mensaje)
    )
    es_continuacion = (
        intent_result.intent in (Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO, Intent.CONFUSO_OTRO)
        and not pide_info_nueva
        and nivel_en_msg is None
        and not es_contenido_pregunta  # una pregunta de contenido NO acepta la visita
    )
    funnel = decidir_funnel(
        capt,
        es_continuacion=es_continuacion,
        nivel_en_msg=(nivel_en_msg.value if nivel_en_msg is not None else None),
        pide_info_nueva=pide_info_nueva,
        en_agendado=(capt.fase_agendado == FaseAgendado.AGENDANDO),
        umbral=settings.umbral_empuje,
        beats_usados=capt.beats_venta_usados,
    )
    if nivel_en_msg is not None and capt.nivel_buscado_actual != nivel_en_msg:
        # Cambio de nivel (o primera vez). Si había un grado de OTRO nivel, lo descartamos
        # para que no se mezcle (bug real: tras "mejor secundaria", el "primero" quedaba
        # como "1° de Kinder" porque el nivel seguía en kinder). Solo se borra si el grado
        # no corresponde al nivel nuevo (no pisamos un grado dado en el MISMO mensaje).
        if capt.nivel_buscado_actual is not None and capt.hijos and capt.hijos[0].grado:
            if nivel_en_msg.value not in capt.hijos[0].grado.lower():
                capt.hijos[0].grado = None
        capt.nivel_buscado_actual = nivel_en_msg
    capt.stage_venta = funnel.stage
    capt.turnos_valor = funnel.turnos_valor
    # El funnel pidió el grado/edad → marcar para capturar el grado SUELTO el próximo turno.
    if funnel.pedir_grado:
        capt.pendiente_grado_funnel = True
        # Fija el nivel EXACTO sobre el que se preguntó (para que "primero" se ate a ese
        # nivel y no a uno viejo repuesto por el extractor) — bug cambio-tema.
        capt.pendiente_grado_nivel = funnel.pedir_grado_nivel
    if funnel.beats_usados:  # marca las ideas dichas para no repetirlas
        capt.beats_venta_usados.extend(funnel.beats_usados)

    # Misma regla: la señal CLARA de visita/cita (regex) cuenta siempre; el intent
    # LLM y una expresión temporal solo si el mensaje NO es una consulta de info
    # ("quiero informes para kinder, costos" → exploración, NO agendar). El papá que
    # ACEPTA el empuje (funnel) también entra al agendado.
    senal_agendado = (
        quiere_agendar_explicito(mensaje)
        or funnel.entrar_agendado
        or (
            not pide_info_exploratoria
            and (
                intent_result.intent == Intent.QUIERE_AGENDAR
                or contiene_expresion_temporal(mensaje)
            )
        )
    )
    entro_agendado_este_turno = False
    if capt.fase_agendado == FaseAgendado.EXPLORANDO and senal_agendado:
        capt.fase_agendado = FaseAgendado.AGENDANDO
        entro_agendado_este_turno = True  # 1er turno → explicar qué es la cita
        log.info("agendado_fase EXPLORANDO→AGENDANDO", extra={"session_id": session_id})
    en_agendado = capt.fase_agendado == FaseAgendado.AGENDANDO

    # 5. Decidir fase del journey
    estado.fase_journey = _decidir_fase(estado, intent_result.intent, es_nueva)
    # PASO 1: la fase pegajosa de agendado MANDA sobre el journey para que el
    # prompt cargue agendado.md (reglas de campus real, 6 datos, no-confirmar)
    # durante toda la colección, sin depender de que el intent dispare.
    if capt.fase_agendado == FaseAgendado.AGENDANDO:
        estado.fase_journey = FaseJourney.AGENDADO
    elif capt.fase_agendado == FaseAgendado.CERRADO:
        estado.fase_journey = FaseJourney.POST_AGENDADO

    # 5bis. Pre-fetch tools cuando el intent lo amerita.
    # Por ahora: campus (Bloque 5.5) + niveles (Bloque 5.6 PASO 2).
    # Inyectamos resultado al prompt como contexto para que Sofía no invente.
    tools_data: dict[str, Any] = {}
    if intent_result.intent == Intent.PREGUNTA_CAMPUS:
        nivel_para_campus = _nivel_para_campus(estado)
        if nivel_para_campus:
            campus_res = await get_campus_para_nivel(nivel_para_campus)
            if campus_res:
                tools_data["campus"] = campus_res.resumen_corto()
                log.info(
                    "tool campus prefetch",
                    extra={"nivel": nivel_para_campus, "campus": campus_res.nombre},
                )

    # COSTOS / HORARIOS / ESTANCIAS — el número lo EMITE el CÓDIGO (no Haiku). Se
    # dispara por PALABRAS CLAVE (det.) además del intent, porque el clasificador LLM
    # falla mensajes bundleados ("kinder, costos y horarios" → confuso_otro). Las
    # `lineas_oferta` se anteponen a la respuesta y `figuras_oferta` arma el set de
    # cifras permitidas para el guard de salida (abajo). Funciona también DURANTE el
    # agendado.
    tipos_oferta = detectar_consulta_oferta(mensaje)
    # "¿cómo es un día?" / "cuántas horas de inglés?" NO son horario escolar → son
    # contenido. No los rutees al callejón "¿de qué nivel necesitas el horario?".
    es_dia_contenido = bool(_DIA_CONTENIDO_RE.search(mensaje)) and not _HORARIO_EXPLICITO_RE.search(
        mensaje
    )
    if intent_result.intent == Intent.PREGUNTA_COSTOS:
        tipos_oferta.add("costos")
    if intent_result.intent == Intent.PREGUNTA_HORARIO and not es_dia_contenido:
        tipos_oferta.add("horario")
    if intent_result.intent == Intent.PREGUNTA_ESTANCIAS:
        tipos_oferta.add("estancias")
    if es_dia_contenido:
        tipos_oferta.discard("horario")  # que lo conteste el funnel/Haiku como contenido
    # "¿la robótica/el francés tienen costo extra?" → NO es la colegiatura; van incluidos.
    # Que Haiku lo aclare desde la KB en vez de soltar la tabla de precios del nivel.
    if "costos" in tipos_oferta and _MATERIA_INCLUIDA_RE.search(mensaje):
        tipos_oferta.discard("costos")
    # "¿cuánto cuesta el horario extendido/estancia?" → el costo es de la ESTANCIA, no la
    # colegiatura. Sin nivel mencionado, descartamos costos para no soltar el menú de nivel.
    if (
        "estancias" in tipos_oferta
        and "costos" in tipos_oferta
        and nivel_buscado_de_mensaje(mensaje) is None
    ):
        tipos_oferta.discard("costos")
    if grado_horario_resuelto:  # el grado suelto re-dispara el horario, ya con el grado
        tipos_oferta.add("horario")
    if grado_costos_resuelto:  # el grado suelto re-dispara el precio, ya con el grado
        tipos_oferta.add("costos")
    # Si el papá nombró el nivel en el mensaje ("para kinder, costos") y el estado
    # aún no lo tiene, fíjalo → la oferta emite SOLO ese nivel, no la tabla completa.
    if tipos_oferta and capt.nivel_buscado_actual is None:
        nivel_msg = nivel_buscado_de_mensaje(mensaje)
        if nivel_msg is not None:
            capt.nivel_buscado_actual = nivel_msg
    lineas_oferta: list[str] = (
        await _construir_oferta(estado, tipos_oferta, mensaje) if tipos_oferta else []
    )
    # ANTI-INVENTO / ADAPTACIÓN: temas fuera de la KB (psicólogo, comedor, # alumnos, examen)
    # o de personalidad (tímido) → respuesta determinística que NO inventa y NO suelta el menú
    # de precios. Tiene PRIORIDAD: sobreescribe la oferta mal ruteada (comedor→estancia, etc.).
    if not en_agendado:
        _esp = _respuesta_especial(mensaje)
        if _esp:
            lineas_oferta = [_esp]
    # RESPUESTAS PROACTIVAS code-emitidas (Bloque B-3) cuando NO hay consulta de oferta,
    # el FUNNEL no actuó, y Haiku quedaría suelto (se quedaba seco / rebotaba "estoy por
    # acá"). El nivel suelto ahora lo maneja el FUNNEL (Etapa 1, sin precio). Solo fuera
    # del agendado y para intents NO sustantivos:
    #   - saludo REPETIDO (no el primero) → reorienta a pedir nivel.
    #   - confuso sin consulta ni datos → línea de reorientación útil.
    # Papá pide hablar con una PERSONA / rechaza al "robot" → respuesta CÁLIDA (sin
    # menú robótico, sin revelar IA). Alta prioridad; no aplica en plena colección.
    if (
        not lineas_oferta
        and capt.fase_agendado != FaseAgendado.AGENDANDO
        and quiere_persona_humana(mensaje)
    ):
        lineas_oferta = [_RESPUESTA_PERSONA]

    if (
        not lineas_oferta
        and not en_agendado
        and funnel.hint is None
        and not funnel.entrar_agendado
        and not es_contenido_pregunta  # una pregunta de contenido la responde el grado
        and intent_result.intent not in _PREGUNTAS_SUSTANTIVAS
    ):
        # ¿La extracción capturó ALGÚN dato del papá este turno? Si sí, NO reorientamos.
        extraccion_con_datos = any(
            [
                extraccion.nombre_papa,
                extraccion.email_papa,
                extraccion.telefono,
                extraccion.nivel_buscado,
                extraccion.nombre_hijo,
                extraccion.edad_hijo,
                extraccion.grado_hijo,
                extraccion.escuela_actual,
                extraccion.diagnostico_hijo,
            ]
        )
        if intent_result.intent == Intent.SALUDO_INICIAL and not es_nueva:
            lineas_oferta = [_REORIENTA_SALUDO]
        elif intent_result.intent == Intent.CONFUSO_OTRO and not extraccion_con_datos:
            lineas_oferta = [_REORIENTA_GENERAL]

    # ORDINAL BAJO AMBIGUO ("segundo", "tercero", "2°" SOLO) sin nivel → el grado 1-3 existe
    # en Kinder, Primaria Y Secundaria. Pedimos el nivel (el grado ya quedó en el estado; al
    # decir "primaria" el funnel canoniza "2° de Primaria" y da el contenido). Evita el
    # "😊"/"Perfecto." sin nada.
    if (
        not lineas_oferta
        and funnel.hint is None
        and nivel_en_msg is None
        and capt.nivel_buscado_actual is None
        and _ORDINAL_BAJO_AMBIGUO_RE.match(mensaje or "")
    ):
        lineas_oferta = [
            "¡Perfecto! Para contarte justo lo de su grado, ¿en qué nivel va: "
            "Kinder, Primaria o Secundaria? 😊"
        ]

    # FUNNEL gracia: Etapa 2 con beats AGOTADOS (hint None pero hay CTA de empuje) →
    # se reduce con gracia emitiendo solo la CTA por código (sin Haiku ni mensaje vacío).
    if not lineas_oferta and funnel.hint is None and funnel.cta and not en_agendado:
        lineas_oferta = [funnel.cta]

    figuras_oferta: set[str] = set()
    for _ln in lineas_oferta:
        figuras_oferta |= extraer_figuras(_ln)
    if lineas_oferta:
        log.info(
            "oferta_emitida_por_codigo",
            extra={"session_id": session_id, "tipos": sorted(tipos_oferta)},
        )

    # 5quater. Handler de QUIERE_AGENDAR (Bloque C.1). Si el papá quiere
    # agendar, intentamos extraer fecha/hora, verificar disponibilidad y
    # (si todo cuadra) crear la cita en pendiente + notificar Lily. El
    # resultado se inyecta como hint al user message del LLM para que Sofía
    # responda con su tono.
    # PASO 1 (2026-05-29): mientras la fase pegajosa esté en AGENDANDO, el handler
    # determinístico corre CADA turno (no solo cuando el intent dispara). Así
    # colecta los slots de día/hora + 6 datos de forma fragmentada y cierra solo
    # cuando los tiene TODOS — el cierre lo decide el código, no Haiku.
    # PAUSA DE INFO EN AGENDADO: si el papá pregunta info/contenido mientras hay cita en
    # proceso (paso día/hora), NO lo enruto como respuesta de fecha. PRECEDENCIA: una
    # FECHA/HORA válida le GANA a la pausa (nunca brincar una fecha buena por mencionar
    # un grado). Si no parsea como fecha/hora y es info nueva (o hay nivel en estado para
    # responder contenido del grado), pauso: respondo y re-ofrezco la visita.
    pausa_info_agendado = False
    reoferta_visita: str | None = None
    hint_pausa_contenido: str | None = None
    en_agendando = capt.fase_agendado == FaseAgendado.AGENDANDO

    # (A) DATA (costos/horario/estancia) DURANTE el agendado (paso día/hora): da el dato
    # por código + re-oferta, sin colectar fecha. (La fecha/hora válida ya ganó arriba.)
    if (
        en_agendando
        and (capt.cita_fecha_slot is None or capt.cita_hora_slot is None)
        and not quiere_agendar_explicito(mensaje)
        and not entro_agendado_este_turno
        and bool(tipos_oferta)
        and not es_fecha_valida
        and not es_hora_valida
    ):
        pausa_info_agendado = True
        reoferta_visita = _reoferta_visita(capt, now)
        log.info("pausa_data_en_agendado", extra={"session_id": session_id})

    # (B) PREGUNTA DE CONTENIDO del grado (en VALOR o AGENDADO): Haiku responde el
    # contenido del grado + re-oferta. NUNCA se toma como aceptar la visita. Usa el
    # nivel del estado si el mensaje no lo trae (caso "Que se fortalece").
    elif es_contenido_pregunta:
        nivel_c = nivel_en_msg or capt.nivel_buscado_actual
        if nivel_c is not None:
            h0 = capt.hijo_efectivo()
            grado_c = h0.grado if (h0 and h0.grado) else None
            hint_c, beats_c = hint_contenido(nivel_c.value, grado_c, capt.beats_venta_usados)
            reoferta_visita = _reoferta_visita(capt, now) if en_agendando else _REOFERTA_VALOR
            pausa_info_agendado = en_agendando  # skip del handler solo si ya está agendando
            if hint_c:  # hay beats nuevos → Haiku redacta el contenido
                hint_pausa_contenido = hint_c
                capt.beats_venta_usados.extend(beats_c)
                log.info("pausa_contenido_grado", extra={"session_id": session_id})
            else:  # beats agotados → reconoce la pregunta (recap de lo visto) + re-oferta
                recap = recap_beats_vistos(capt.beats_venta_usados)
                if recap:
                    lineas_oferta = [
                        f"{recap}, y eso lo terminas de sentir en persona. {reoferta_visita}"
                    ]
                else:
                    lineas_oferta = [reoferta_visita]
                log.info("pausa_contenido_agotado_reoferta", extra={"session_id": session_id})

    appointment_handler: AppointmentHandlerResult | None = None
    if en_agendado and not pausa_info_agendado:
        try:
            appointment_handler = await handle_appointment_intent(
                mensaje, estado, ultimo_assistant=ultimo_assistant_msg, now=now
            )
        except Exception as exc:  # resiliente: nunca rompemos el turno
            log.warning(
                "appointment_handler error",
                extra={"session_id": session_id, "error": str(exc)},
            )

    # 5ter. Pre-fetch niveles_por_edad cuando el papá pregunta por una etapa
    # específica (infants, baby, cubs, toddlers, preschool/kinder) o pide rangos
    # de edad. Ataca el bug "Sofía dice 'Infants 3-12 meses' en vez de 18m-2a".
    nivel_keyword = _detectar_nivel_en_mensaje(mensaje)
    if nivel_keyword:
        nivel_res = await consultar_edades_de_nivel(nivel_keyword)
        if nivel_res:
            tools_data["nivel_edad"] = (
                f"{nivel_res.nombre_display}: {nivel_res.rango_legible()}. "
                f"{nivel_res.descripcion or ''}".strip()
            )
            log.info(
                "tool niveles prefetch",
                extra={"keyword": nivel_keyword, "nivel": nivel_res.nivel},
            )

    # 6. Componer prompt
    system_blocks = build_system_blocks(estado)

    # 7. Convertir historial (cargado en paso 3) al formato Anthropic
    messages_llm = [
        {"role": _normalize_role(m["role"]), "content": m["content"]} for m in historial
    ]

    # 7bis. Bloque 5.7 ATAQUE 2 — Detectar "respuesta corta al turno previo".
    # Si el papá responde con un mensaje muy corto (≤15 chars) que es
    # confirmación/continuación del turno previo de Sofía, inyectamos contexto
    # explícito para que NO recite info no pedida.
    # (ultimo_assistant_msg se computó en el paso 3, junto al historial.)
    hay_turno_previo_assistant_local = ultimo_assistant_msg is not None
    es_resp_corta = es_respuesta_corta_al_turno_previo(
        mensaje, hay_turno_previo_assistant=hay_turno_previo_assistant_local
    )
    # Override del intent del LLM si la heurística determinística matchea:
    if es_resp_corta and intent_result.intent != Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO:
        log.info(
            "intent_override → RESPUESTA_CORTA_AL_TURNO_PREVIO (heurístico)",
        )
        intent_result = IntentResult(
            intent=Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
            confidence=1.0,
            razonamiento_breve="override heurístico",
        )

    # 7ter. Si llamamos tools o detectamos respuesta-corta, inyectamos hints.
    mensaje_para_llm = mensaje
    # Hint del FLUJO DE VENTA (Etapa 1 enganche / Etapa 2 valor+empuje): el CÓDIGO
    # manda el contenido y el momento del empuje; Haiku solo redacta. Tiene prioridad
    # sobre el hint genérico de respuesta-corta.
    if funnel.hint:
        mensaje_para_llm = f"{mensaje_para_llm}\n\n{funnel.hint}"
    elif hint_pausa_contenido:  # pausa en agendado: Haiku responde el contenido del grado
        mensaje_para_llm = f"{mensaje_para_llm}\n\n{hint_pausa_contenido}"
    if (
        funnel.hint is None
        and hint_pausa_contenido is None
        and intent_result.intent == Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO
        and ultimo_assistant_msg
    ):
        ultimo_trunc = ultimo_assistant_msg.strip()[:300]
        mensaje_para_llm += (
            "\n\n[CONTEXTO CRÍTICO: el papá acaba de responder con un mensaje "
            f"muy corto ({mensaje!r}). Es una continuación al turno PREVIO tuyo "
            f'donde dijiste: "{ultimo_trunc}".\n'
            "Tu respuesta DEBE: "
            "1) tratar el mensaje del papá como respuesta a TU pregunta o afirmación anterior. "
            "2) NO recitar información nueva no pedida. "
            "3) Si la respuesta corta cierra un loop conversacional, avanza el journey 1 paso pequeño. "
            "4) Si la respuesta es ambigua, pregunta UNA cosa breve.]"
        )

    if tools_data:
        tool_hint_lines = [
            "[DATO OFICIAL del sistema — úsalo EXACTO. NO cambies ni inventes números "
            "de costo/horario/estancia: si un número no está aquí, defiérelo a Miss "
            "Lili, NO lo inventes:]"
        ]
        for tool_name, data in tools_data.items():
            tool_hint_lines.append(f"- {tool_name}: {data}")
        mensaje_para_llm = f"{mensaje_para_llm}\n\n" + "\n".join(tool_hint_lines)

    # Hint de OFERTA: el sistema ya le muestra al papá las cifras exactas (las
    # antepone el código). Haiku NO debe escribir números — se eliminan en el guard.
    if lineas_oferta:
        mensaje_para_llm = (
            f"{mensaje_para_llm}\n\n[DATOS DE OFERTA: el sistema YA le muestra al papá "
            f"las cifras exactas de costo/horario/estancia que pidió (se insertan "
            f"automáticamente arriba de tu respuesta). TÚ no escribas NINGÚN número, "
            f"monto, '$' ni horario — cualquier cifra que pongas se ELIMINARÁ. Solo "
            f"agrega 1-2 frases cálidas de contexto y, si quieres, UNA pregunta breve. "
            f"NO repitas los datos.]"
        )

    # Hint del handler de agendado (Bloque C.1)
    if appointment_handler is not None and appointment_handler.hint_para_prompt:
        mensaje_para_llm = f"{mensaje_para_llm}\n\n{appointment_handler.hint_para_prompt}"
        log.info(
            "appointment_flow",
            extra={
                "session_id": session_id,
                "acciones": appointment_handler.acciones,
                "appointment_id": appointment_handler.appointment_id,
                "lead_id": appointment_handler.lead_id,
            },
        )

    messages_llm.append({"role": "user", "content": mensaje_para_llm})

    # 8. Persistir mensaje del usuario (antes de la llamada LLM)
    await _persist_user_message(repo, estado, mensaje)

    # FIX 2/3 (2026-05-29) + FIX (2026-06-02): ¿hay una cita REALMENTE registrada?
    # Solo entonces Sofía puede confirmarla; si no, el validator
    # `no_confirma_cita_inexistente` bloquea el ghost-close.
    # Por-intento: durante AGENDANDO solo cuenta la cita creada ESTE turno (el
    # `cita_agendada` pegajoso de un cierre VIEJO ya no desarma el validador). En
    # POST-cierre (CERRADO) sí se permite referenciar la cita real existente.
    cita_realmente_registrada = bool(
        (appointment_handler is not None and appointment_handler.appointment_id is not None)
        or (
            estado.estado_capturado.fase_agendado == FaseAgendado.CERRADO
            and estado.estado_capturado.cita_agendada
        )
    )

    # 9. Llamar a Anthropic con loop de validación + regeneración
    anthropic = get_anthropic()
    max_regen = settings.max_regenerations_per_turn if settings.enable_validators else 0
    # TECHO de longitud para turnos de VENTA/CONTENIDO (Haiku se pasa de largo): ~220
    # tokens. El cap REAL es el recorte a 4 oraciones (abajo).
    es_turno_breve = (
        funnel.hint is not None
        or hint_pausa_contenido is not None
        or (capt.stage_venta == "valor" and capt.fase_agendado != FaseAgendado.AGENDANDO)
    )
    max_tokens_turno = 220 if es_turno_breve else 600
    response_text = ""
    final_report: ValidationReport | None = None
    regenerations = 0
    # Métricas acumuladas (sumamos cada intento)
    tokens_input = 0
    tokens_output = 0
    tokens_cache_read = 0
    tokens_cache_write = 0
    llm_latency = 0
    extra_messages: list[dict[str, Any]] = []  # feedback de validators para reintentos

    # COLECCIÓN DETERMINÍSTICA (2026-06-04): el CÓDIGO es dueño de TODOS los turnos
    # de colección. La pregunta del único campo faltante la genera render_pregunta_campo
    # (plantilla fija) — Haiku NO arma preguntas de colección (no bundlea, no ofrece
    # fin de semana, no improvisa). Haiku solo se invoca para PREGUNTAS SUSTANTIVAS
    # del papá (costos/metodología/objeción); tras responder, el código RETOMA con
    # la pregunta del campo (se anexa abajo, tras el loop).
    es_coleccion = bool(
        appointment_handler is not None
        and appointment_handler.mensaje_coleccion
        and appointment_handler.appointment_id is None
    )
    intent_sustantivo = intent_result.intent in _PREGUNTAS_SUSTANTIVAS
    # Solo saltamos Haiku cuando NO es pregunta sustantiva: ahí la respuesta ES la
    # pregunta del campo. Si es sustantiva, Haiku contesta y luego anexamos el campo.
    coleccion_directa = es_coleccion and not intent_sustantivo

    # INFO DIRECTA (2026-06-10): en un turno de info puro (pidió costos/horarios/
    # estancias y NO va a agendar), la respuesta la EMITE el CÓDIGO completa: bloque
    # de datos + una línea fija de cierre. Haiku NO se invoca → sin saludo duplicado,
    # sin monólogo de venta, sin sondeo. (En agendado la oferta se maneja distinto.)
    # Dispara también en una PAUSA de info dentro del agendado (DATA: costos/horario/
    # estancia) → emite el dato y re-ofrece la visita, sin perder la cita.
    info_directa = bool(lineas_oferta) and (not en_agendado or pausa_info_agendado)

    llm_started = time.perf_counter()
    for intento in range(max_regen + 1):
        if coleccion_directa:
            response_text = appointment_handler.mensaje_coleccion  # type: ignore[union-attr]
            # PRIMER turno del agendado: explica BREVE qué es la cita de informes
            # (code-emitido, no Haiku) antes de la pregunta del día. No es sondeo.
            if entro_agendado_este_turno:
                response_text = f"{_EXPLICACION_CITA_INFORMES}\n\n{response_text}"
            final_report = None
            log.info(
                "coleccion_directa (pregunta por código, sin Haiku)",
                extra={"session_id": session_id, "acciones": appointment_handler.acciones},
            )
            break
        if info_directa:
            cuerpo_info = "\n\n".join(lineas_oferta)
            # En pausa de agendado, cierra RE-OFRECIENDO la visita; si no, el cierre
            # genérico (salvo que el bloque ya termine en pregunta).
            cierre = reoferta_visita if pausa_info_agendado else _CIERRE_INFO
            if cuerpo_info.rstrip().endswith("?"):
                response_text = cuerpo_info
            else:
                response_text = f"{cuerpo_info}\n\n{cierre}"
            final_report = None
            log.info(
                "info_directa (datos por código, sin Haiku)",
                extra={"session_id": session_id, "tipos": sorted(tipos_oferta)},
            )
            break
        try:
            message = await anthropic.chat(
                system_blocks=system_blocks,
                messages=messages_llm + extra_messages,
                model=settings.anthropic_model_principal,
                max_tokens=max_tokens_turno,
                temperature=0.55,
            )
        except Exception as exc:
            log.error(
                "anthropic chat failed",
                extra={"error": str(exc), "session_id": session_id, "intento": intento},
            )
            raise

        response_text = _extract_text_response(message)
        usage = getattr(message, "usage", None)
        tokens_input += getattr(usage, "input_tokens", 0) or 0
        tokens_output += getattr(usage, "output_tokens", 0) or 0
        tokens_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        tokens_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

        if not settings.enable_validators:
            final_report = None
            break

        # Bloque 5.7 ATAQUE 1: pasar mensajes_papa + fase_journey para los
        # validators heurísticos (no_inventa_datos, no_bullets_descubrimiento)
        mensajes_papa_lista = [
            m["content"] for m in historial if (m.get("role") or "").lower() in ("user", "human")
        ]
        final_report = run_all_validators(
            respuesta=response_text,
            estado=estado.estado_capturado,
            intent=intent_result.intent,
            tools_called=[],  # Bloque 4 introducirá tools reales
            frases_usadas=estado.frases_usadas,
            mensajes_papa=[*mensajes_papa_lista, mensaje],
            fase_journey=estado.fase_journey,
            cita_realmente_registrada=cita_realmente_registrada,
        )

        # Loggear warnings (NO disparan regeneración; severity="warning")
        warnings_map = final_report.warnings_map
        if warnings_map:
            log.warning(
                "validator_warnings",
                extra={
                    "session_id": session_id,
                    "intento": intento,
                    "warnings": warnings_map,
                },
            )

        if final_report.all_passed:
            break

        # Si todavía hay presupuesto, prepara reintento con feedback
        if intento < max_regen:
            feedback = final_report.feedback_para_regenerar()
            log.info(
                "validator_failed_regenerating",
                extra={
                    "session_id": session_id,
                    "intento": intento + 1,
                    "fallas": list(final_report.failed_map.keys()),
                },
            )
            # Inyectar respuesta previa + feedback como secuencia user/assistant
            extra_messages = [
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": feedback or "Mejora tu respuesta anterior."},
            ]
            regenerations += 1
        else:
            # Sin más presupuesto — enviamos la última versión y loggeamos
            log.warning(
                "validator_warning_max_regen_reached",
                extra={
                    "session_id": session_id,
                    "fallas": list(final_report.failed_map.keys()),
                },
            )

    llm_latency = int((time.perf_counter() - llm_started) * 1000)

    # GUARDS de TEXTO LIBRE de Haiku (Bloque B) — ENFORZAR la ESTRATEGIA, no solo la
    # cantidad. Quita venezolanismos y recorta preguntas. El tope de preguntas es
    # DINÁMICO para que Sofía no interrogue:
    #   - turno de OFERTA (dio costos/horario/estancia) → 0 preguntas de sondeo
    #     enganchadas (da el dato y cierra; nada de "¿qué es lo que más te importa?").
    #   - ya gastó su ÚNICA pregunta de discovery en la conversación → 0.
    #   - si no → el tope normal (1).
    # Las preguntas de DATOS del agendado las emite el código aparte, no cuentan.
    # info_directa ya es 100% código (datos + cierre) → no se sanea ni se le quita
    # la pregunta de cierre.
    # Turno de VENTA (funnel) o de CONTENIDO en pausa de agendado: Haiku redacta el
    # valor/contenido; la pregunta de cierre la pone el CÓDIGO (CTA / re-oferta).
    turno_venta = funnel.hint is not None or hint_pausa_contenido is not None
    cta_codigo = funnel.cta or (reoferta_visita if hint_pausa_contenido else None)
    en_funnel = capt.stage_venta == "valor" and not en_agendado
    if not coleccion_directa and not info_directa:
        # En VENTA/CONTENIDO o dentro del FUNNEL Haiku escribe SOLO el valor (0
        # preguntas); la pregunta de cierre la pone el CÓDIGO → nunca se cuela el
        # descubrimiento ("¿qué te importa?", "¿ya lo trabajaba?").
        if turno_venta or en_funnel:
            max_preg = 0
        elif lineas_oferta or capt.discovery_pregunta_hecha:
            max_preg = 0
        else:
            max_preg = settings.max_preguntas_por_turno
        response_text = sanear_texto_libre_haiku(response_text, max_preguntas=max_preg)
        # En OFERTA/FUNNEL/CONTENIDO: NADA de sondeo de descubrimiento.
        if lineas_oferta or en_funnel or hint_pausa_contenido:
            response_text = sanear_sondeo(response_text)
        # SIEMPRE: quita una oración final incompleta (Haiku cortado por el tope de tokens)
        # ANTES de anexar cualquier CTA — si no, queda algo colgado a media frase
        # ("…antes de salir (a las 2:00") en medio del mensaje. maximo alto = solo limpia
        # la cola, no recorta el cuerpo.
        response_text = recortar_oraciones(response_text, maximo=999)
        if turno_venta:
            response_text = sanear_cifras_ajenas(response_text, set())
            # CAP REAL de longitud: recorta a 4 oraciones COMPLETAS (nunca a media
            # frase) ANTES de anexar la CTA del código.
            response_text = recortar_oraciones(response_text, maximo=4)
            if cta_codigo:  # el CÓDIGO cierra con la pregunta de la etapa/re-oferta
                response_text = f"{response_text.rstrip()}\n\n{cta_codigo}"
        # Re-enganche ligero en PAUSA (respondió un dato dentro del funnel y quedó sin
        # pregunta) → hacia la VISITA, nunca descubrimiento.
        elif en_funnel and "?" not in response_text:
            response_text = (
                f"{response_text.rstrip()}\n\n¿Quieres que te cuente algo más o "
                "agendamos una visita? 😊"
            )
        # El cupo de discovery NO aplica dentro del funnel.
        if (
            not lineas_oferta
            and not turno_venta
            and not en_funnel
            and not en_agendado
            and "?" in response_text
        ):
            capt.discovery_pregunta_hecha = True

    # OFERTA — el número lo EMITE el código. Saneamos SOLO la parte de Haiku (borra
    # cualquier $monto/hora no oficial) y anteponemos las líneas con las cifras
    # exactas. Se hace ANTES de anexar la pregunta de colección (que es código y NO
    # debe sanearse — su ejemplo de formato lleva horas válidas). No aplica en cierre.
    # (info_directa ya armó la respuesta 100% por código en el loop → no re-anteponer.)
    _es_cierre = appointment_handler is not None and appointment_handler.appointment_id is not None
    if lineas_oferta and not _es_cierre and not info_directa:
        cuerpo = sanear_cifras_ajenas(response_text, figuras_oferta)
        response_text = "\n\n".join(lineas_oferta) + (f"\n\n{cuerpo}" if cuerpo else "")
        log.info(
            "oferta_prepend+guard",
            extra={"session_id": session_id, "figuras": sorted(figuras_oferta)},
        )

    # COLECCIÓN + pregunta sustantiva: Haiku contestó la duda del papá; el CÓDIGO
    # RETOMA con la pregunta del campo faltante (determinística, un solo campo) →
    # Haiku nunca arma la pregunta de colección.
    if es_coleccion and intent_sustantivo and appointment_handler.mensaje_coleccion:
        response_text = f"{response_text.rstrip()}\n\n{appointment_handler.mensaje_coleccion}"
        log.info(
            "coleccion_retoma_tras_sustantiva",
            extra={"session_id": session_id, "intent": intent_result.intent.value},
        )

    cost = calculate_cost(
        model=settings.anthropic_model_principal,
        input_tokens=tokens_input,
        output_tokens=tokens_output,
        cache_write_tokens=tokens_cache_write,
        cache_read_tokens=tokens_cache_read,
    )

    # 9bis. Registrar frases munición usadas en esta respuesta (para anti-repetición futura)
    nuevas_frases = extraer_frases_municion_usadas(response_text)
    for frase in nuevas_frases:
        estado.marcar_frase_usada(frase)

    # 10. D.4 (Gaby 27-may): cuando el handler registró cita pendiente,
    # reemplazamos la respuesta del LLM con el mensaje determinístico
    # (texto oficial de Gaby) que incluye día+fecha, hora, campus, dirección
    # y Maps. El LLM a veces omitía el link de Maps aún con el hint
    # instruyéndolo copiar-pegar.
    llm_response_original = response_text
    if (
        appointment_handler is not None
        and appointment_handler.appointment_id is not None
        and appointment_handler.appointment_datetime is not None
    ):
        fecha_dt = appointment_handler.appointment_datetime.to_datetime()
        if fecha_dt is not None:
            response_text = render_registration_message(
                fecha_hora=fecha_dt,
                campus=appointment_handler.campus,
                canal=estado.canal.value,  # FIX 2: Maps clickeable según canal
                now=now,  # etiqueta hoy/mañana en la confirmación
            )
            # PASO 1: el CÓDIGO cierra la fase pegajosa al crear la cita. El
            # appointment_id es el RESULTADO de completar los slots, no un
            # requisito previo. CERRADO impide reabrir el agendado.
            campus_nombre = (
                appointment_handler.campus.nombre if appointment_handler.campus else None
            )
            if campus_nombre in ("Campus 1", "Campus 2"):
                estado.marcar_agendado(fecha_dt, campus_nombre)
            else:
                estado.agendado = True
                estado.estado_capturado.cita_agendada = True
                estado.estado_capturado.fecha_cita = fecha_dt
            estado.estado_capturado.fase_agendado = FaseAgendado.CERRADO
            log.info(
                "appointment_registration_override+CERRADO",
                extra={
                    "session_id": session_id,
                    "appointment_id": appointment_handler.appointment_id,
                    "campus_id": appointment_handler.campus_id,
                    "had_maps_url": bool(
                        appointment_handler.campus and appointment_handler.campus.google_maps_url
                    ),
                },
            )

    # 10bis. SALUDO REPETIDO: si NO es el primer turno (ya hubo un mensaje del assistant)
    # y Haiku abre otra vez con la presentación formal, la recortamos (se veía raro que
    # Sofía se re-presentara a mitad de la charla). Si al recortar quedaría vacío, se deja.
    if ultimo_assistant_msg:
        _sin_saludo = _SALUDO_REPETIDO_RE.sub("", response_text, count=1).lstrip()
        if _sin_saludo and _sin_saludo != response_text:
            response_text = _sin_saludo
            log.info("saludo_repetido_recortado", extra={"session_id": session_id})

    # 10ter. ANTI-LOOP: si la respuesta REPITE el núcleo del turno anterior (el papá re-
    # preguntó algo que no pudimos detallar y reemitiríamos el mismo bloque), NO repetimos
    # — ESCALAMOS a Lily / visita. Antes solo anteponía "Como te comentaba" y seguía el loop
    # 3-5 veces (queja #1 real de los papás: "me repites lo mismo, esto parece un bot tonto").
    if (
        not _es_cierre
        and ultimo_assistant_msg
        and _nucleo_respuesta(response_text) == _nucleo_respuesta(ultimo_assistant_msg)
        and _nucleo_respuesta(response_text) != _nucleo_respuesta(_ESCALACION_LOOP)
    ):
        response_text = _ESCALACION_LOOP
        log.info("anti_loop_escalacion", extra={"session_id": session_id})

    # 10quinquies. SALVAVIDAS — NUNCA enviar vacío ni un ACK MUERTO ("Qué bueno.", "Claro,
    # con gusto te cuento.", "Perfecto.") sin nada más. Bug real (Gaby): "Me encanta" →
    # mensaje EN BLANCO; "Así es" → "Qué bueno." y muerto. Si pasa, movemos la conversación.
    _rt = (response_text or "").strip()
    _ack_muerto = bool(
        re.fullmatch(
            r"(?:qu[ée]\s+bueno|claro(?:,?\s*(?:con\s+gusto\s*)?te\s+cuento)?|perfecto|"
            r"excelente|entendido|de\s+acuerdo|genial|s[íi]|ok|aj[áa]|muy\s+bien)"
            r"[.!\s😊🙂🌱💛]*",
            _rt,
            re.IGNORECASE,
        )
    )
    if (not _rt or _ack_muerto) and not _es_cierre:
        response_text = (
            "¡Qué gusto! 😊 ¿Te gustaría que te cuente de algún nivel o grado en específico, "
            "los costos, o prefieres que agendemos una visita para que conozcas el colegio?"
        )
        log.info("salvavidas_respuesta_muerta", extra={"session_id": session_id})

    # 11. Persistir respuesta del assistant
    await _persist_assistant_message(
        repo,
        estado,
        response_text,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost,
        model_used=settings.anthropic_model_principal,
        cache_hit=tokens_cache_read > 0,
        latency_ms=llm_latency,
    )

    # 12. Persistir turn_log
    turn_number = await repo.count_turns(session_id)
    prompt_compuesto = "\n\n---\n\n".join(b.get("text", "") for b in system_blocks)
    validators_passed = final_report.passed_map if final_report else {}
    validators_failed = final_report.failed_map if final_report else {}
    await repo.insert_turn_log(
        session_id=session_id,
        turn_number=turn_number,
        user_message=mensaje,
        intent=intent_result.intent.value,
        prompt_compuesto=prompt_compuesto[:50000],  # cap por si acaso
        llm_response=llm_response_original,
        final_response=response_text,
        validators_passed=validators_passed,
        validators_failed=validators_failed,
        regenerations=regenerations,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cached=tokens_cache_read,
        cost_usd=cost,
        latency_ms=llm_latency,
        model_used=settings.anthropic_model_principal,
        metadata={
            "stage_venta": capt.stage_venta,
            "turnos_valor": capt.turnos_valor,
            "pregunta_info_nueva": pide_info_nueva,
            "empuje_inyectado": funnel.empuje,
            "etapa_hint": "venta" if funnel.hint else None,
        },
    )

    # 13. Persistir estado actualizado
    await repo.upsert_conversation(estado)

    total_latency = int((time.perf_counter() - started) * 1000)
    log.info(
        "turn_completed",
        extra={
            "session_id": session_id,
            "turn_number": turn_number,
            "intent": intent_result.intent.value,
            "fase": estado.fase_journey.value,
            "regenerations": regenerations,
            "validators_failed": list(validators_failed.keys()) if validators_failed else [],
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "tokens_cache_read": tokens_cache_read,
            "cost_usd": float(cost),
            "latency_ms": total_latency,
        },
    )

    validators_warnings = list(final_report.warnings_map.keys()) if final_report else []
    return TurnResult(
        response=response_text,
        session_id=session_id,
        fase_journey=estado.fase_journey,
        intent=intent_result.intent,
        cost_usd=cost,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cached=tokens_cache_read,
        latency_ms=total_latency,
        model_used=settings.anthropic_model_principal,
        turn_number=turn_number,
        validators_failed=list(validators_failed.keys()) if validators_failed else [],
        validators_warnings=validators_warnings,
        regenerations=regenerations,
    )


# ============================================================
# Helpers
# ============================================================


_NIVEL_KEYWORDS = (
    "infants",
    "infant",
    "baby",
    "babies",
    "cubs",
    "cub",
    "toddlers",
    "toddler",
    "preschool",
    "maternal",
    "kinder",
)


def _detectar_nivel_en_mensaje(mensaje: str) -> str | None:
    """Devuelve la primera keyword de nivel detectada en el mensaje, o None.

    Usado para decidir si hacemos pre-fetch a `consultar_edades_de_nivel`.
    Solo dispara cuando el papá pregunta concretamente por un nivel/etapa.
    """
    msg = mensaje.lower()
    for kw in _NIVEL_KEYWORDS:
        # Detectar como palabra (no como substring de otra palabra)
        if re.search(rf"\b{kw}\b", msg):
            return kw
    return None


def _nivel_para_campus(estado: EstadoConversacion) -> str | None:
    """Mapea el nivel buscado a la key usada en la tabla `campus.niveles`.

    Campus 1 atiende `maternal`, `kinder`, `primaria_baja`.
    Campus 2 atiende `primaria_alta`, `secundaria`.

    Si el papá habla de "primaria" sin grado, asumimos primaria_baja (Campus 1).
    Tabla seed-ada con `primaria_baja`, `primaria_alta`, etc.
    """
    capt = estado.estado_capturado
    nivel = capt.nivel_buscado_actual
    if nivel is None and capt.hijos:
        nivel = capt.hijos[0].nivel
    if nivel is None:
        return None
    nivel_val = nivel.value if hasattr(nivel, "value") else str(nivel)

    # Mapear primaria genérica → primaria_baja como default seguro
    if nivel_val == "primaria":
        # Si tenemos edad, podemos distinguir: ≤9 → baja, ≥10 → alta
        edad: int | None = None
        for h in capt.hijos:
            if h.edad is not None:
                edad = h.edad
                break
        if edad is not None and edad >= 10:
            return "primaria_alta"
        return "primaria_baja"
    return nivel_val


async def _ultimos_dos_turnos_resumen(repo: Any, session_id: str) -> str | None:
    """Devuelve los 2 últimos mensajes (user+assistant) como contexto del feedback."""
    try:
        rows = await repo.list_recent_messages(session_id, limit=2)
    except Exception:
        return None
    if not rows:
        return None
    parts = [f"[{r.get('role')}] {r.get('content', '')[:300]}" for r in rows]
    return "\n".join(parts)


async def _persist_user_message(repo: Any, estado: EstadoConversacion, mensaje: str) -> None:
    await repo.insert_message(
        session_id=estado.session_id,
        role="user",
        content=mensaje,
    )


async def _persist_assistant_message(
    repo: Any,
    estado: EstadoConversacion,
    response_text: str,
    **metrics: Any,
) -> None:
    await repo.insert_message(
        session_id=estado.session_id,
        role="assistant",
        content=response_text,
        **metrics,
    )


def _decidir_fase(
    estado: EstadoConversacion,
    intent: Intent,
    es_nueva: bool,
) -> FaseJourney:
    """Heurística simple para mapear intent → fase. NO toca el estado si ya está agendado."""
    if estado.agendado:
        return FaseJourney.POST_AGENDADO

    if estado.fase_journey == FaseJourney.BIENVENIDA and not es_nueva:
        # Tras la primera respuesta, avanza a descubrimiento por default
        nueva = FaseJourney.DESCUBRIMIENTO
    else:
        nueva = estado.fase_journey

    # Override por intención
    if intent == Intent.QUIERE_AGENDAR:
        nueva = FaseJourney.AGENDADO
    elif intent in (
        Intent.OBJECION_CARO,
        Intent.OBJECION_FLEXIBLE,
        Intent.OBJECION_TAREA,
        Intent.OBJECION_OTRA,
    ):
        nueva = FaseJourney.OBJECIONES
    elif intent in (
        Intent.PREGUNTA_COSTOS,
        Intent.PREGUNTA_HORARIO,
        Intent.PREGUNTA_ESTANCIAS,
        Intent.PREGUNTA_CAMPUS,
    ):
        nueva = FaseJourney.INFORMACION
    elif intent in (
        Intent.PREGUNTA_METODOLOGIA,
        Intent.PREGUNTA_NIVEL,
        Intent.MENCIONA_DIAGNOSTICO,
    ):
        if estado.fase_journey in (FaseJourney.BIENVENIDA, FaseJourney.DESCUBRIMIENTO):
            nueva = FaseJourney.EDUCACION
        else:
            nueva = estado.fase_journey

    return nueva


def _extract_text_response(message: Any) -> str:
    """Anthropic devuelve content como lista de bloques. Concatena los text blocks."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip() or "(sin respuesta)"


def _normalize_role(role: str) -> str:
    """Normaliza role del historial al formato Anthropic ('user'|'assistant')."""
    role = (role or "").lower()
    if role in ("human", "user"):
        return "user"
    if role in ("ai", "assistant"):
        return "assistant"
    return "user"
