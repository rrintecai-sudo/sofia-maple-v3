"""Handler de QUIERE_AGENDAR (Bloque C.1 PASO 5).

Encapsula el flujo cuando Sofía detecta que el papá quiere agendar:
1. Extraer fecha/hora del mensaje (gpt-4o-mini).
2. Si falta info, devolver hint para que Sofía pida aclaración.
3. Si hay fecha:
    - Verificar disponibilidad en lily_availability + appointments
    - Si disponible: ensure_lead → create_appointment(pendiente) →
      emit_event → advance_stage → enviar email a Lily
    - Si NO: devolver hint con 3 alternativas para que Sofía las proponga

Devuelve un AppointmentHandlerResult con:
- hint_para_prompt: texto que el orchestrator inyecta al user message
  del LLM. Sofía responde con su tono usando ese contexto.
- acciones: lista de pasos que se ejecutaron (auditoría).
- lead_id, appointment_id si se crearon.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings, get_settings
from app.core.appointment_extractor import (
    TZ_MONTERREY,
    AppointmentDateTime,
    elegir_dia_de_opciones_llm,
    elegir_opcion_dia,
    es_confirmacion,
    extract_datetime,
    extraer_fecha_explicita,
    extraer_fecha_relativa,
    extraer_hora_de_numero_suelto,
    extraer_hora_simple,
    extraer_proximo_dia_semana,
    fecha_humana_solo_dia,
    motivo_ajuste_fecha_relativa,
)
from app.core.appointment_messages import (
    art_dia,
    formato_opciones_dia,
    render_pregunta_campo,
)
from app.core.campus_resolver import resolve_campus_from_estado
from app.core.state import EstadoCapturado, EstadoConversacion, NivelEducativo
from app.core.state_extractor import extraer_grado_simple
from app.integrations.appointments import create_appointment
from app.integrations.events import emit_event
from app.integrations.leads import (
    advance_stage_if_lower,
    create_lead,
    get_lead_by_session,
    update_lead,
)
from app.notifications.email import (
    render_cita_pendiente_email,
    render_confirmacion_email_papa,
    send_email,
)
from app.tools.availability_checker import (
    AvailabilityResult,
    evaluar_dia,
    is_slot_available,
    proximos_dias_habiles,
    resumen_disponibilidad,
)
from app.tools.campus import CampusResult, get_campus_by_id
from app.tools.niveles import derivar_nivel_grado_de_edad

log = logging.getLogger(__name__)


@dataclass
class AppointmentHandlerResult:
    """Resultado del handler. El orchestrator inyecta `hint_para_prompt`
    al user message del LLM para guiar la respuesta de Sofía.
    """

    hint_para_prompt: str
    acciones: list[str] = field(default_factory=list)
    lead_id: int | None = None
    appointment_id: int | None = None
    campus_id: int | None = None
    campus: CampusResult | None = None
    appointment_datetime: AppointmentDateTime | None = None
    availability: AvailabilityResult | None = None
    # Pregunta determinística por el ÚNICO campo faltante (un solo campo, plantilla
    # fija). El orchestrator la usa como respuesta SALVO que el papá haga una
    # pregunta sustantiva (costos/campus/objeción) — ahí responde Haiku.
    mensaje_coleccion: str | None = None


# ============================================================
# Helpers internos
# ============================================================


def _formato_fecha_humana(dt: datetime) -> str:
    """Formato breve para mostrar al papá ('lunes 26 de mayo, 10:00')."""
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    return f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def _formatear_alternativas(alts: list[datetime]) -> str:
    return "; ".join(_formato_fecha_humana(a) for a in alts) or "(ninguna cercana)"


def _formatear_horas(slots: list[datetime]) -> str:
    """'8:00, 9:00, 10:00' — solo las horas (mismo día)."""
    return ", ".join(f"{s.hour}:{s.minute:02d}" for s in slots) or "(por confirmar)"


# ============================================================
# Pregunta dirigida por CÓDIGO (FIX 2026-06-02): el gate decide QUÉ pedir; Haiku
# solo FRASEA el único campo faltante. Nunca elige ni re-pregunta un slot lleno.
# ============================================================


def _resumen_capturado(
    estado: EstadoConversacion, *, fecha_slot: str | None, hora_slot: str | None
) -> str:
    """Lista legible de los datos YA capturados, para que Haiku NO los re-pida
    y, al confirmar, los ECHE en vez de pedirlos en blanco."""
    capt = estado.estado_capturado
    h = capt.hijo_efectivo()
    partes: list[str] = []
    if h and h.nombre:
        partes.append(f"nombre del niño/a: {h.nombre}")
    if h and h.edad is not None:
        partes.append(f"edad: {h.edad} años")
    if h and h.grado:
        partes.append(f"grado: {h.grado}")
    if capt.nombre_papa:
        partes.append(f"tu nombre: {capt.nombre_papa}")
    if capt.email_papa:
        partes.append(f"correo: {capt.email_papa}")
    if capt.telefono:
        partes.append(f"teléfono: {capt.telefono}")
    if fecha_slot:
        partes.append(f"día: {fecha_humana_solo_dia(fecha_slot) or fecha_slot}")
    if hora_slot:
        partes.append(f"hora: {hora_slot}")
    return "; ".join(partes)


def _instruccion_un_campo(pedir: str, resumen: str, *, extra: str = "") -> str:
    """Hint estándar: Haiku pregunta SOLO `pedir`, enumera lo ya capturado y tiene
    PROHIBIDO inventar opciones o re-pedir datos llenos."""
    ya = (
        f" Datos que YA tienes (NO los vuelvas a pedir; si el papá confirma, "
        f"menciónalos en vez de pedirlos en blanco): {resumen}."
        if resumen
        else ""
    )
    return (
        f"[FLUJO AGENDADO — pregunta ÚNICAMENTE por: {pedir}. UNA sola pregunta, "
        f"breve y cálida.{extra}{ya} NO pidas ningún otro dato, NO ofrezcas "
        f"modalidades (presencial / videollamada / por teléfono) NI inventes "
        f"opciones que el sistema no te dio. NO confirmes la cita todavía.]"
    )


# Orden de prioridad para pedir el ÚNICO dato del lead que falte.
_ETIQUETA_CAMPO: dict[str, str] = {
    "nombre del hijo": "el nombre completo del niño/a",
    "edad del hijo": "la edad del niño/a",
    "grado escolar del hijo": "el grado escolar del niño/a",
    "tu nombre": "tu nombre",
    "correo electrónico": "tu correo electrónico",
    "número de celular": "tu número de celular",
}
# Mapea el faltante a una CLAVE persistida en estado (ultimo_campo_pedido), para
# capturar la respuesta SUELTA del siguiente turno por su propia vía.
_CLAVE_CAMPO: dict[str, str] = {
    "nombre del hijo": "nombre_hijo",
    "edad del hijo": "edad",
    "grado escolar del hijo": "grado",
    "tu nombre": "nombre_papa",
    "correo electrónico": "correo",
    "número de celular": "telefono",
}


# Grado CANÓNICO: "2° de Kinder", "1° de Primaria", etc. Un grado que NO matchea
# (ej. "kinder", "primaria", "3 de kinder") se considera parcial → la deducción
# por edad lo sobreescribe (FIX (2) 2026-06-02).
_GRADO_CANONICO_RE = re.compile(r"^[1-6]°\s+de\s+\w", re.IGNORECASE)


def datos_lead_faltantes(estado: EstadoConversacion) -> list[str]:
    """D.3 (Lily 2026-05-27): los 6 datos requeridos ANTES de crear la cita.

    Devuelve la lista de campos legibles que faltan. Si está vacía, está OK
    para agendar. Lily definió en la reunión 27-may los datos exactos:

      1. Nombre del alumno (hijo)
      2. Edad del hijo
      3. Grado escolar del hijo  (excepto Maternal, donde la edad ya define el grupo)
      4. Nombre del papá/mamá
      5. Correo electrónico del papá
      6. Número celular del papá

    Para Maternal, el "grado" no aplica como tal — la edad y el nivel ya
    definen el sub-grupo (Cubs/Baby/Infants/Toddlers). En ese caso no se
    pide grado.
    """
    capt = estado.estado_capturado
    faltantes: list[str] = []

    # FIX (d): hijo CONSOLIDADO (fusiona huérfanos de sesión contaminada), no hijos[0].
    primer_hijo = capt.hijo_efectivo()
    nombre_hijo = primer_hijo.nombre if primer_hijo else None
    edad_hijo = primer_hijo.edad if primer_hijo else None
    grado_hijo = primer_hijo.grado if primer_hijo else None
    nivel_hijo = (primer_hijo.nivel if primer_hijo else None) or capt.nivel_buscado_actual

    if not nombre_hijo:
        faltantes.append("nombre del hijo")
    if edad_hijo is None:
        faltantes.append("edad del hijo")
    # FIX 1 (2026-06-01): el grado se DEDUCE de la edad (no se pregunta). Solo es
    # faltante si NO hay grado NI edad (sin edad no hay de dónde deducir) y el
    # nivel no es maternal (maternal usa la edad como criterio, sin grado).
    es_maternal = nivel_hijo is not None and nivel_hijo.value == "maternal"
    if not grado_hijo and not es_maternal and edad_hijo is None:
        faltantes.append("grado escolar del hijo")
    if not capt.nombre_papa:
        faltantes.append("tu nombre")
    if not capt.email_papa:
        faltantes.append("correo electrónico")
    if not capt.telefono:
        faltantes.append("número de celular")

    return faltantes


def _nivel_para_leads(estado: EstadoConversacion) -> str | None:
    """Mapea el nivel del estado al enum lead_nivel.

    NivelEducativo (maternal/kinder/primaria/secundaria) ya coincide con
    los valores válidos. Si el papá habla de prepa, también es válido.
    """
    capt = estado.estado_capturado
    nivel = capt.nivel_buscado_actual
    if nivel is None:
        h = capt.hijo_efectivo()
        nivel = h.nivel if h else None
    if nivel is None:
        return None
    return nivel.value


def _primer_hijo(estado: EstadoConversacion) -> tuple[str | None, int | None]:
    """Devuelve (nombre_hijo, edad_hijo) del hijo CONSOLIDADO (FIX (d)), o (None, None)."""
    h = estado.estado_capturado.hijo_efectivo()
    if h is None:
        return None, None
    return h.nombre, h.edad


# ============================================================
# Ensure lead — crea si tenemos nombre, si no devuelve None
# ============================================================


def _primer_hijo_grado(estado: EstadoConversacion) -> str | None:
    h = estado.estado_capturado.hijo_efectivo()  # FIX (d): hijo consolidado
    return h.grado if h else None


async def _ensure_lead_para_cita(estado: EstadoConversacion, *, settings: Settings) -> int | None:
    """Obtiene o crea el lead vinculado a esta sesión.

    D.3 (Lily 2026-05-27): asume que `datos_lead_faltantes(estado)` ya validó
    que los 6 datos estén presentes. Si no, hay un bug en el caller.
    """
    capt = estado.estado_capturado
    nombre_hijo, edad_hijo = _primer_hijo(estado)
    grado_hijo = _primer_hijo_grado(estado)
    nivel = _nivel_para_leads(estado)

    existing = await get_lead_by_session(estado.session_id, settings=settings)
    if existing:
        # FIX (2026-06-02): al REUSAR un lead (misma sesión/teléfono persistente),
        # la conversación NUEVA es la fuente de verdad. Antes solo se rellenaban
        # campos None, así que una edad/nivel viejos (ej. 3/maternal) NO se
        # actualizaban aunque el papá ahora dijera "4 años" → quedaba desfasado.
        # Ahora actualizamos cuando el dato nuevo EXISTE y DIFIERE del viejo.
        updates: dict = {}
        if nombre_hijo and existing.child_name != nombre_hijo:
            updates["child_name"] = nombre_hijo
        if edad_hijo is not None and existing.child_age != edad_hijo:
            updates["child_age"] = edad_hijo
        if grado_hijo and existing.child_grade != grado_hijo:
            updates["child_grade"] = grado_hijo
        if nivel and existing.nivel != nivel:
            updates["nivel"] = nivel
        if capt.nombre_papa and existing.parent_name != capt.nombre_papa:
            updates["parent_name"] = capt.nombre_papa
        if capt.telefono and existing.parent_phone != capt.telefono:
            updates["parent_phone"] = capt.telefono
        if capt.email_papa and existing.parent_email != capt.email_papa:
            updates["parent_email"] = capt.email_papa
        if updates:
            await update_lead(existing.id, updates, settings=settings)
        return existing.id

    parent_name = capt.nombre_papa
    if not parent_name:
        return None

    return await create_lead(
        parent_name=parent_name,
        channel=estado.canal.value,
        conversation_session_id=estado.session_id,
        parent_phone=capt.telefono,
        parent_email=capt.email_papa,
        child_name=nombre_hijo,
        child_age=edad_hijo,
        child_grade=grado_hijo,
        nivel=nivel,
        settings=settings,
    )


# ============================================================
# Handler principal
# ============================================================


def _rescatar_de_propuesta(
    capt: EstadoCapturado, propuesta: str, *, now: datetime | None = None
) -> list[str]:
    """FIX (b) 2026-06-01 — rescate por confirmación GENERAL.

    Cuando el papá confirma, captura al slot lo que Sofía PROPUSO en su último
    turno (`propuesta`): fecha, hora y grado. Solo rellena slots vacíos, nunca
    sobreescribe. (El campus NO se rescata: lo deriva el código del grado/nivel.)
    Devuelve la lista de campos rescatados (para auditoría/log).
    """
    rescatados: list[str] = []
    if not capt.cita_fecha_slot:
        f = extraer_fecha_explicita(propuesta, now)
        if f:
            capt.cita_fecha_slot = f
            rescatados.append("fecha")
    if not capt.cita_hora_slot:
        h = extraer_hora_simple(propuesta)
        if h:
            capt.cita_hora_slot = h
            rescatados.append("hora")
    if capt.hijos and not capt.hijos[0].grado:
        g, _niv = extraer_grado_simple(propuesta)
        if g:
            capt.hijos[0].grado = g
            rescatados.append("grado")
    return rescatados


def _nivel_de_grado_canonico(grado: str | None) -> NivelEducativo | None:
    """'1° de Primaria' → NivelEducativo.PRIMARIA. None si no se reconoce."""
    if not grado:
        return None
    g = grado.lower()
    for clave, nivel in (
        ("kinder", NivelEducativo.KINDER),
        ("primaria", NivelEducativo.PRIMARIA),
        ("secundaria", NivelEducativo.SECUNDARIA),
        ("maternal", NivelEducativo.MATERNAL),
    ):
        if clave in g:
            return nivel
    return None


async def _consolidar_y_derivar_hijo(
    capt: EstadoCapturado, *, settings: Settings | None = None
) -> tuple[str, str | None, str] | None:
    """Consolida los hijos en uno y, POR EDAD, deduce nivel/grado SOLO cuando el
    papá no declaró un grado canónico (Política A, 2026-06-04). Devuelve
    (categoria, grado, nombre_display) derivado, o None si no derivó.
    """
    hijo = capt.hijo_efectivo()
    if hijo is None:
        return None
    derivado: tuple[str, str | None, str] | None = None
    # POLÍTICA A (2026-06-04): un grado CANÓNICO declarado por el papá ("1° de
    # Primaria", incl. "primero de primaria" ya canonicalizado) MANDA — la edad NO
    # lo sobreescribe. La derivación por edad solo aplica si NO hay grado canónico
    # (grado parcial tipo "kinder", o sin grado). FIX (2) sigue vigente para parciales.
    grado_canonico = bool(hijo.grado) and bool(_GRADO_CANONICO_RE.match(hijo.grado.strip()))
    # El nivel se infiere del GRADO declarado (no de la edad) para no contradecirlo.
    if grado_canonico and hijo.nivel is None:
        hijo.nivel = _nivel_de_grado_canonico(hijo.grado)
    if hijo.edad is not None and (hijo.nivel is None or not grado_canonico):
        nivel_pref = (
            hijo.nivel.value
            if hijo.nivel
            else (capt.nivel_buscado_actual.value if capt.nivel_buscado_actual else None)
        )
        derivado = await derivar_nivel_grado_de_edad(
            hijo.edad, nivel_preferido=nivel_pref, settings=settings
        )
        if derivado:
            categoria, grado, _disp = derivado
            if hijo.nivel is None:
                try:
                    hijo.nivel = NivelEducativo(categoria)
                except ValueError:
                    pass
            if grado and not grado_canonico:  # sobreescribe el parcial/malformado
                hijo.grado = grado
    # Consolida: colapsa huérfanos/fragmentos en un solo hijo enriquecido.
    capt.hijos = [hijo]
    return derivado


# "en la mañana"/"por la tarde"/"al mediodía" → hora concreta dentro del horario de
# Lily (8-15h). Solo se usa en el paso de la hora. Si el slot estuviera ocupado, la
# validación de disponibilidad lo descarta y propone alternativas reales.
_FRANJA_MANANA_RE = re.compile(r"\b(?:en|por|de|a)\s+la\s+ma[ñn]ana\b", re.IGNORECASE)
_FRANJA_TARDE_RE = re.compile(
    r"\b(?:en|por|de|a)\s+la\s+tarde\b|\bmedio\s*d[íi]a\b|\bal\s+mediod[íi]a\b", re.IGNORECASE
)


def _franja_a_hora(mensaje: str) -> str | None:
    """'en la mañana' → '09:00'; 'en la tarde'/'mediodía' → '13:00'. None si no hay franja."""
    t = (mensaje or "").lower()
    if _FRANJA_MANANA_RE.search(t):
        return "09:00"
    if _FRANJA_TARDE_RE.search(t):
        return "13:00"
    return None


async def handle_appointment_intent(
    mensaje: str,
    estado: EstadoConversacion,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    ultimo_assistant: str | None = None,
) -> AppointmentHandlerResult:
    """Procesa el flujo de agendado cuando intent == QUIERE_AGENDAR.

    Llamado por el orchestrator antes de invocar al LLM. Retorna un
    AppointmentHandlerResult cuyo `hint_para_prompt` se inyecta al user
    message para que Sofía responda con su tono propio guiada por el
    estado real del agendado.

    `ultimo_assistant`: el último mensaje de Sofía, para el rescate por
    confirmación (FIX (b)).
    """
    settings = settings or get_settings()

    capt = estado.estado_capturado

    # FIX (b) 2026-06-01: rescate por confirmación. Si el papá confirma ("sí",
    # "dale", "correcto") y Sofía propuso un valor en su último turno, lo
    # capturamos al slot AUNQUE el extractor LLM haya fallado el mensaje del papá
    # (typo "10a", fecha que solo Sofía escribió, etc.).
    if ultimo_assistant and es_confirmacion(mensaje):
        rescatados = _rescatar_de_propuesta(capt, ultimo_assistant, now=now)
        if rescatados:
            log.info("rescate_por_confirmacion", extra={"campos": rescatados})

    # FIX 1 (2026-06-01): consolida el/los hijos y DEDUCE nivel/grado de la edad
    # (no se pregunta). El grado deducido queda en el hijo; el gate ya no pregunta
    # el grado, así que no se usa el valor de retorno aquí.
    await _consolidar_y_derivar_hijo(capt, settings=settings)

    # 1. Extraer fecha/hora del mensaje y FUNDIRLA en los slots persistentes.
    # PASO 1 (2026-05-29): en conversación fragmentada el papá da el día en un
    # turno y la hora/datos en otros. Guardamos lo resuelto en slots para no
    # "olvidar" la fecha entre turnos.
    #
    # FIX (2026-06-01): la HORA se desanida de la fecha — se llena aunque venga
    # en un mensaje aparte ("2pm" sin fecha). Antes solo se guardaba la hora si
    # el MISMO mensaje traía fecha, así que la hora suelta se perdía y la cita
    # nunca cerraba (bug real de la prueba de Oscar). Además, un fallback
    # determinístico cubre cuando el extractor LLM no resuelve la hora suelta.
    # FIX (2026-06-02): la FECHA se resuelve DETERMINÍSTICAMENTE primero — el LLM no
    # debe ser load-bearing. "5 de junio" → explícita; "hoy"/"mañana" → relativa;
    # "el viernes"/"lunes" → próxima ocurrencia. Si el mensaje ACTUAL trae una fecha,
    # MANDA (cubre cambios tipo "mejor el lunes"); si no, NO toca el slot capturado.
    # NUEVO ENFOQUE (2026-06-11): el código PROPONE fechas concretas y el papá elige.
    # `elegir_opcion_dia` matchea su respuesta contra las opciones ofrecidas (ordinal/
    # nombre del día/número). Las relativas quedan como respaldo, no como vía principal.
    campo_pedido_prev = capt.ultimo_campo_pedido
    fecha_det = (
        extraer_fecha_explicita(mensaje, now)
        or elegir_opcion_dia(mensaje, capt.opciones_dia_propuestas)
        or extraer_fecha_relativa(mensaje, now)
        or extraer_proximo_dia_semana(mensaje, now)
    )
    # CLAUDE-CONDUCE (demo): el resolver rígido no entiende respuestas vagas como
    # "esta semana"/"cualquiera"/"la más pronto" → se quedaba en loop re-preguntando.
    # Si YA ofrecimos fechas concretas y el papá está en el paso del día, dejamos que
    # el LLM elija UNA de esas fechas (el código manda QUÉ fechas existen; Claude solo
    # mapea la intención). Reemplaza decenas de regex frágiles por un juicio.
    if not fecha_det and capt.opciones_dia_propuestas and campo_pedido_prev == "dia":
        elegida = await elegir_dia_de_opciones_llm(mensaje, capt.opciones_dia_propuestas, now=now)
        if elegida:
            fecha_det = elegida
            log.info(
                "dia_elegido_por_llm",
                extra={"fecha": elegida, "mensaje": mensaje[:60]},
            )
    if fecha_det:
        capt.cita_fecha_slot = fecha_det
        capt.opciones_dia_propuestas = []  # ya eligió → limpia las opciones

    # Si "hoy" se movió a otro día (cierre/fin de semana), guardamos la RAZÓN para
    # decírsela al papá — no saltar de día en silencio.
    motivo_fecha = motivo_ajuste_fecha_relativa(mensaje, now) if fecha_det else None

    appt_dt = await extract_datetime(mensaje, now=now)
    if appt_dt.es_alta_confianza and appt_dt.fecha and capt.cita_fecha_slot is None:
        # El LLM solo RELLENA la fecha si el resolver determinístico no la fijó.
        capt.cita_fecha_slot = appt_dt.fecha

    # HORA: la fija el código desde tokens DETERMINISTAS. La hora del LLM solo se
    # acepta cuando ESTAMOS en el paso de la hora — NUNCA en el paso del día (evita
    # que "hoy" se vuelva una hora alucinada → "esa hora fuera de horario").
    en_paso_hora = campo_pedido_prev == "hora"
    if capt.cita_hora_slot is None:
        hora_det = extraer_hora_simple(mensaje)
        if not hora_det and en_paso_hora:
            hora_det = extraer_hora_de_numero_suelto(mensaje)
        if not hora_det and en_paso_hora and appt_dt.es_alta_confianza and appt_dt.hora:
            hora_det = appt_dt.hora
        # "en la mañana"/"por la tarde" sin hora exacta → franja concreta (solo en el
        # paso de la hora, para no confundir "mañana"=día). Evita el loop "no te entendí".
        if not hora_det and en_paso_hora:
            hora_det = _franja_a_hora(mensaje)
        if hora_det:
            capt.cita_hora_slot = hora_det

    fecha_slot = capt.cita_fecha_slot
    hora_slot = capt.cita_hora_slot

    # 1a. Sin día en los slots → el CÓDIGO PROPONE fechas concretas disponibles y el
    # papá elige (ya no se parsea "hoy/mañana"). Si no hay disponibilidad (o falla),
    # cae a la pregunta abierta con el horario real.
    if fecha_slot is None:
        resumen = await resumen_disponibilidad(settings)
        dias_opciones = await proximos_dias_habiles(settings=settings, now=now, cantidad=3)
        opciones_texto = formato_opciones_dia(dias_opciones, now=now) if dias_opciones else None
        capt.opciones_dia_propuestas = [d.date().isoformat() for d in dias_opciones]
        horario_linea = (
            f" El horario REAL de Lily es: {resumen}. NUNCA digas 'todos los días' "
            f"ni inventes disponibilidad; ofrece SOLO dentro de ese horario."
            if resumen
            else ""
        )
        resumen_cap = _resumen_capturado(estado, fecha_slot=None, hora_slot=hora_slot)
        # "esta semana"/"la siguiente" (respuesta al empuje) NO es un error: re-presenta
        # las fechas concretas sin "Perdón, no te entendí bien".
        menciona_semana = bool(re.search(r"\bsemana\b", (mensaje or "").lower()))
        es_reintento = campo_pedido_prev == "dia" and not menciona_semana
        capt.ultimo_campo_pedido = "dia"
        opciones_hint = (
            f" Ofrece EXACTAMENTE estas fechas, sin inventar otras: {opciones_texto}."
            if opciones_texto
            else f" NO inventes una fecha.{horario_linea}"
        )
        return AppointmentHandlerResult(
            hint_para_prompt=_instruccion_un_campo(
                "qué DÍA le queda mejor para la visita",
                resumen_cap,
                extra=opciones_hint,
            ),
            mensaje_coleccion=render_pregunta_campo(
                "dia",
                opciones_dia=opciones_texto,
                horario=(f"Atendemos {resumen}." if resumen else None),
                reintento=es_reintento,
            ),
            acciones=["propone_dias" if opciones_texto else "extract_failed"],
            appointment_datetime=appt_dt,
        )

    # 1b. Hay día pero falta la hora. FIX (2026-06-02): validamos el DÍA contra el
    # AHORA real + disponibilidad de Lily ANTES de pedir la hora. Así no se ofrece
    # un día imposible (hoy ya cerró, día pasado, no laborable, lleno) y no se
    # inventa "todos los días".
    if hora_slot is None:
        dia_resuelto = fecha_humana_solo_dia(fecha_slot, now) or "ese día"
        try:
            dia_dt = datetime.strptime(fecha_slot, "%Y-%m-%d").replace(tzinfo=TZ_MONTERREY)
        except ValueError:
            dia_dt = None

        eval_dia = await evaluar_dia(dia_dt, settings=settings, now=now) if dia_dt else None

        if eval_dia is not None and eval_dia.reason != "supabase_error" and not eval_dia.available:
            # El día NO es reservable → descartar el slot y proponer alternativas REALES.
            capt.cita_fecha_slot = None
            capt.cita_hora_slot = None
            alts_str = _formatear_alternativas(eval_dia.alternativas)
            motivo = {
                "fecha_pasada": (
                    f"{dia_resuelto} ya no es posible (hoy ya pasó el horario de atención "
                    f"o ese día ya pasó)"
                ),
                "dia_no_laborable": f"ese día ({dia_resuelto}) Lily no atiende",
                "slot_ocupado": f"ese día ({dia_resuelto}) ya está lleno",
            }.get(eval_dia.reason, f"{dia_resuelto} no está disponible")
            horario = f" Horario real de Lily: {eval_dia.resumen}." if eval_dia.resumen else ""
            # El CÓDIGO re-pide el día (solo lun-vie 8-15), nunca fin de semana.
            motivo_corto = {
                "fecha_pasada": "Ese día ya no es posible.",
                "dia_no_laborable": "Ese día no atendemos (solo lun-vie).",
                "slot_ocupado": "Ese día ya está lleno.",
            }.get(eval_dia.reason, "Ese día no se puede.")
            horario_lv = f"Atendemos {eval_dia.resumen}." if eval_dia.resumen else None
            return AppointmentHandlerResult(
                hint_para_prompt=(
                    f"[FLUJO AGENDADO — {motivo}.{horario} Dilo con claridad (NADA de "
                    f"'no tengo claro') y propón EXACTAMENTE estas opciones, sin inventar "
                    f"otras: {alts_str}. Una sola pregunta breve: '¿te queda alguna?']"
                ),
                mensaje_coleccion=render_pregunta_campo(
                    "dia", horario=horario_lv, motivo=motivo_corto
                ),
                acciones=[f"dia_no_disponible:{eval_dia.reason}"],
                appointment_datetime=appt_dt,
                availability=eval_dia,
            )

        # Día válido (o no se pudo verificar) → pedir la hora con la fecha RESUELTA
        # y, si las tenemos, las horas reales libres de ese día.
        horas_linea = ""
        if eval_dia is not None and eval_dia.available and eval_dia.alternativas:
            horas_linea = (
                f" Ese día Lily tiene libre: {_formatear_horas(eval_dia.alternativas[:6])}. "
                f"Ofrécele esas, NO inventes otras."
            )
        resumen_cap = _resumen_capturado(estado, fecha_slot=fecha_slot, hora_slot=None)
        es_reintento = campo_pedido_prev == "hora"  # ya habíamos pedido la hora → no repetir igual
        capt.ultimo_campo_pedido = "hora"
        horas_libres = (
            _formatear_horas(eval_dia.alternativas[:6])
            if eval_dia is not None and eval_dia.available and eval_dia.alternativas
            else None
        )
        return AppointmentHandlerResult(
            hint_para_prompt=_instruccion_un_campo(
                f"a qué HORA le queda mejor {art_dia(dia_resuelto)}{dia_resuelto}",
                resumen_cap,
                extra=(
                    f" Usa EXACTAMENTE ese día ({dia_resuelto}); NO lo recalcules ni "
                    f"inventes otro.{horas_linea}"
                ),
            ),
            mensaje_coleccion=render_pregunta_campo(
                "hora",
                dia=dia_resuelto,
                horas_libres=horas_libres,
                reintento=es_reintento,
                motivo=(
                    f"{motivo_fecha}, te propongo {art_dia(dia_resuelto)}{dia_resuelto}."
                    if motivo_fecha
                    else None
                ),
            ),
            acciones=["missing_time"],
            appointment_datetime=appt_dt,
        )

    # Construir el datetime efectivo desde los slots (acumulado entre turnos).
    try:
        fecha_dt = datetime.strptime(f"{fecha_slot} {hora_slot}", "%Y-%m-%d %H:%M").replace(
            tzinfo=TZ_MONTERREY
        )
    except ValueError:
        fecha_dt = None
    if fecha_dt is None:
        # Slot corrupto → limpiarlo para volver a pedir, no quedar en bucle.
        capt.cita_fecha_slot = None
        capt.cita_hora_slot = None
        capt.ultimo_campo_pedido = "dia"
        return AppointmentHandlerResult(
            hint_para_prompt=(
                "[FLUJO AGENDADO — no pude convertir la fecha del papá. Pídele que "
                "te confirme día y hora exactos.]"
            ),
            mensaje_coleccion=render_pregunta_campo("dia"),
            acciones=["parse_failed"],
            appointment_datetime=appt_dt,
        )

    # 2. Verificar disponibilidad
    avail = await is_slot_available(fecha_dt, duracion_minutos=60, settings=settings, now=now)
    fecha_humana = _formato_fecha_humana(fecha_dt)

    if not avail.available:
        alts_str = _formatear_alternativas(avail.alternativas)
        horario = f" Horario real de Lily: {avail.resumen}." if avail.resumen else ""
        # FIX (2026-06-02): cuando el día/hora no sirve, descartar el slot para no
        # reofrecer lo mismo, y dar mensaje CLARO + alternativa (nada de "no sé").
        if avail.reason != "supabase_error":
            capt.cita_hora_slot = None
            if avail.reason in ("fecha_pasada", "dia_no_laborable"):
                capt.cita_fecha_slot = None

        horario_lv = f"Atendemos {avail.resumen}." if avail.resumen else None
        msg_col: str | None = None  # pregunta por código del campo a re-pedir
        if avail.reason == "fecha_pasada":
            hint = (
                f"[FLUJO AGENDADO — {fecha_humana} ya pasó (respecto a la hora actual). "
                f"NO lo ofrezcas.{horario} Propón EXACTAMENTE: {alts_str}. "
                f"Una sola pregunta breve.]"
            )
            capt.ultimo_campo_pedido = "dia"
            msg_col = render_pregunta_campo("dia", horario=horario_lv, motivo="Ese día ya pasó.")
        elif avail.reason == "dia_no_laborable":
            hint = (
                f"[FLUJO AGENDADO — ese día ({fecha_humana}) Lily NO atiende.{horario} "
                f"Propón EXACTAMENTE estas alternativas, sin inventar otras: {alts_str}. "
                f"Una sola pregunta breve: '¿te queda alguna de estas?']"
            )
            capt.ultimo_campo_pedido = "dia"
            msg_col = render_pregunta_campo(
                "dia", horario=horario_lv, motivo="Ese día no atendemos (solo lun-vie)."
            )
        elif avail.reason == "fuera_de_horario":
            hint = (
                f"[FLUJO AGENDADO — esa hora ({fecha_humana}) está FUERA del horario de "
                f"atención.{horario} Dilo claro (NADA de 'no tengo claro') y ofrece la "
                f"opción más cercana dentro del horario. Propón EXACTAMENTE: {alts_str}. "
                f"Una sola pregunta breve.]"
            )
            capt.ultimo_campo_pedido = "hora"
            msg_col = render_pregunta_campo(
                "hora", motivo="Esa hora está fuera de nuestro horario.", horario=horario_lv
            )
        elif avail.reason == "slot_ocupado":
            hint = (
                f"[FLUJO AGENDADO — ese horario ({fecha_humana}) ya está ocupado.{horario} "
                f"Propón EXACTAMENTE estas alternativas: {alts_str}. Una sola pregunta breve.]"
            )
            capt.ultimo_campo_pedido = "hora"
            _horas_oc = _formatear_horas(avail.alternativas[:6]) if avail.alternativas else None
            msg_col = render_pregunta_campo(
                "hora", horas_libres=_horas_oc, motivo="Esa hora ya está ocupada."
            )
        else:  # supabase_error — transitorio, deja a Haiku pedir confirmación
            hint = (
                "[FLUJO AGENDADO — no pude verificar disponibilidad ahora. "
                "Pídele al papá que te confirme la fecha y dile que en breve "
                "le respondes.]"
            )
        return AppointmentHandlerResult(
            hint_para_prompt=hint,
            mensaje_coleccion=msg_col,
            acciones=[f"availability:{avail.reason}"],
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 3. D.3 (Lily 2026-05-27) — verificar los 6 datos requeridos del lead
    # ANTES de crear la cita. Sin los 6, no registramos cita: Sofía pide
    # los que faltan de forma conversacional.
    faltantes = datos_lead_faltantes(estado)
    if faltantes:
        # El CÓDIGO elige el ÚNICO dato a pedir (el primero faltante por prioridad);
        # Haiku solo lo frasea. Nunca elige qué pedir ni re-pregunta un slot lleno.
        pedir = _ETIQUETA_CAMPO.get(faltantes[0], faltantes[0])
        clave = _CLAVE_CAMPO.get(faltantes[0])
        es_reintento = clave is not None and clave == campo_pedido_prev  # mismo campo otra vez
        capt.ultimo_campo_pedido = clave
        resumen = _resumen_capturado(estado, fecha_slot=fecha_slot, hora_slot=hora_slot)
        nombre_hijo_actual, _ = _primer_hijo(estado)
        return AppointmentHandlerResult(
            hint_para_prompt=_instruccion_un_campo(pedir, resumen),
            mensaje_coleccion=render_pregunta_campo(
                clave, nombre_hijo=nombre_hijo_actual, reintento=es_reintento
            )
            if clave
            else None,
            acciones=[f"missing_lead_data:{','.join(faltantes)}"],
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 4. Datos completos — ahora sí, ensure_lead
    lead_id = await _ensure_lead_para_cita(estado, settings=settings)
    if lead_id is None:
        capt.ultimo_campo_pedido = "nombre_papa"
        return AppointmentHandlerResult(
            hint_para_prompt=(
                f"[FLUJO AGENDADO — la fecha ({fecha_humana}) está disponible, "
                "pero aún no sabemos el nombre del papá. Pregúntaselo amable "
                "en UNA oración antes de cerrar la cita: '¿cómo te llamas?']"
            ),
            mensaje_coleccion=render_pregunta_campo("nombre_papa"),
            acciones=["missing_parent_name"],
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 3bis. Resolver campus desde el nivel del hijo (NUNCA preguntar al papá)
    campus_id = resolve_campus_from_estado(estado)
    if campus_id is None:
        # Caso ambiguo (típico: primaria sin grado). Pide grado antes de cerrar.
        nombre_hijo_actual, _ = _primer_hijo(estado)
        capt.ultimo_campo_pedido = "grado"
        return AppointmentHandlerResult(
            hint_para_prompt=(
                f"[FLUJO AGENDADO — la fecha ({fecha_humana}) está disponible, "
                "pero NO podemos asignar campus porque falta el grado del hijo "
                "(Primaria 1°-5° va a Campus 1, Primaria 6° va a Campus 2). "
                "Pregunta el grado exacto en UNA oración breve. NO inventes campus.]"
            ),
            mensaje_coleccion=render_pregunta_campo("grado", nombre_hijo=nombre_hijo_actual),
            acciones=["missing_grado"],
            lead_id=lead_id,
            appointment_datetime=appt_dt,
            availability=avail,
        )
    campus = await get_campus_by_id(campus_id, settings=settings)

    # 4. Crear la cita en pendiente (con campus_id resuelto)
    appointment_id = await create_appointment(
        lead_id=lead_id,
        fecha_hora=fecha_dt,
        duracion_min=60,
        notas=f"Solicitada por Sofía vía {estado.canal.value}. Mensaje del papá: {mensaje[:200]}",
        campus_id=campus_id,
        settings=settings,
    )
    if appointment_id is None:
        return AppointmentHandlerResult(
            hint_para_prompt=(
                "[FLUJO AGENDADO — hubo un problema técnico al registrar la cita. "
                "Pídele disculpas y dile que en breve te confirmamos.]"
            ),
            acciones=["create_appointment_failed"],
            lead_id=lead_id,
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 5-6. Tail post-creación: eventos, stage y correos. TODO esto es BEST-EFFORT
    # — la cita YA está creada. Va envuelto en try/except para que NINGÚN error
    # (red, Resend, Supabase) impida devolver el AppointmentHandlerResult con el
    # appointment_id → el cierre D.4 SIEMPRE se dispara. El correo nunca es
    # load-bearing.
    acciones: list[str] = ["appointment_created"]
    try:
        await emit_event(
            "sofia_appointment_scheduled",
            lead_id=lead_id,
            session_id=estado.session_id,
            description=(
                f"Sofía solicitó cita para {fecha_humana} en "
                f"{campus.nombre if campus else f'campus_id={campus_id}'} (pendiente de aprobación)"
            ),
            metadata={
                "appointment_id": appointment_id,
                "fecha_hora": fecha_dt.isoformat(),
                "canal": estado.canal.value,
                "status": "pendiente",
                "campus_id": campus_id,
            },
            settings=settings,
        )
        acciones.append("event_emitted")

        # advance_stage requiere conocer el stage actual; obtenemos el lead recién
        lead_now = await get_lead_by_session(estado.session_id, settings=settings)
        if lead_now and lead_now.stage != "cita_agendada":
            avanzado = await advance_stage_if_lower(
                lead_id, lead_now.stage, "cita_agendada", settings=settings
            )
            if avanzado:
                acciones.append("stage_advanced")
                await emit_event(
                    "lead_stage_changed",
                    lead_id=lead_id,
                    session_id=estado.session_id,
                    description=f"Stage avanzó de {lead_now.stage} a cita_agendada",
                    metadata={"from": lead_now.stage, "to": "cita_agendada"},
                    settings=settings,
                )

        # 6a. Aviso interno a Lily (cita pendiente).
        nombre_hijo, edad_hijo = _primer_hijo(estado)
        subject, body = render_cita_pendiente_email(
            nombre_papa=estado.estado_capturado.nombre_papa,
            nombre_hijo=nombre_hijo,
            edad_hijo=edad_hijo,
            nivel=_nivel_para_leads(estado),
            fecha_hora_iso=fecha_dt.isoformat(),
            canal=estado.canal.value,
            appointment_id=appointment_id,
            approval_url=settings.appointment_approval_url or None,
        )
        if settings.lily_email:
            await send_email(settings.lily_email, subject, body, settings=settings)
            acciones.append("email_sent_to_lily")
        else:
            await send_email("", subject, body, settings=settings)
            acciones.append("email_skipped_no_recipient")

        # 6b. Correo de CONFIRMACIÓN al PAPÁ (Mensaje 2 de Gaby) — al correo que
        # capturamos. Resend; si falla, send_email NO lanza (solo loggea).
        email_papa = (estado.estado_capturado.email_papa or "").strip()
        if email_papa:
            subj_p, text_p, html_p = render_confirmacion_email_papa(
                nombre_papa=estado.estado_capturado.nombre_papa,
                fecha_hora=fecha_dt,
                campus=campus,
            )
            res_papa = await send_email(email_papa, subj_p, text_p, html=html_p, settings=settings)
            acciones.append("email_papa_sent" if res_papa.delivered else "email_papa_failed")
        else:
            acciones.append("email_papa_skipped_sin_correo")
    except Exception as exc:  # best-effort: NUNCA rompe el cierre de la cita
        log.warning(
            "appointment_tail_error (cita YA creada, se ignora)",
            extra={"appointment_id": appointment_id, "error": str(exc)},
        )

    # 7. Hint final para que Sofía responda al papá
    # El campus se ASIGNA aquí; el LLM debe MENCIONARLO pero NUNCA preguntar
    # cuál campus prefiere.
    campus_nombre = campus.nombre if campus else f"Campus {campus_id}"
    direccion_legible = campus.direccion_legible() if campus else "dirección del campus"
    maps_link = (campus.google_maps_url if campus else "") or ""
    maps_line = f"🗺️ {maps_link}" if maps_link else ""

    hint = (
        f"[FLUJO AGENDADO — la cita quedó REGISTRADA como PENDIENTE de aprobación "
        f"para {fecha_humana} en **{campus_nombre}** (asignado automáticamente por el "
        f"nivel del hijo — NO preguntes ni ofrezcas elegir otro campus). "
        f"Tu respuesta DEBE: "
        f"1) Confirmar que registraste su solicitud (NO digas 'confirmada' ni 'confirmamos'). "
        f"2) Mencionar el campus por nombre Y incluir la dirección y link de Maps EXACTOS "
        f"que te paso debajo. "
        f"3) Decir que en breve le avisamos por este mismo canal ({estado.canal.value}). "
        f"4) Ser cálida y breve, 3-4 oraciones máximo.\n"
        f"\n"
        f"Datos del campus para incluir EXACTOS en tu respuesta (copia, NO reformules):\n"
        f"📍 {direccion_legible}\n"
        f"{maps_line}]"
    )
    # PASO 1: devolvemos el datetime EFECTIVO de los slots (no el del último
    # mensaje, que en cierre fragmentado puede no traer fecha) para que el
    # override D.4 del orchestrator renderice la plantilla con la fecha correcta.
    appointment_dt_efectivo = AppointmentDateTime(
        fecha=fecha_slot, hora=hora_slot, confidence=1.0, razonamiento="slots"
    )
    return AppointmentHandlerResult(
        hint_para_prompt=hint,
        acciones=acciones,
        lead_id=lead_id,
        appointment_id=appointment_id,
        campus_id=campus_id,
        campus=campus,
        appointment_datetime=appointment_dt_efectivo,
        availability=avail,
    )
