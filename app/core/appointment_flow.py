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
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings, get_settings
from app.core.appointment_extractor import (
    AppointmentDateTime,
    extract_datetime,
)
from app.core.state import EstadoConversacion
from app.integrations.appointments import create_appointment
from app.integrations.events import emit_event
from app.integrations.leads import (
    advance_stage_if_lower,
    create_lead,
    get_lead_by_session,
    update_lead,
)
from app.notifications.email import render_cita_pendiente_email, send_email
from app.tools.availability_checker import AvailabilityResult, is_slot_available

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
    appointment_datetime: AppointmentDateTime | None = None
    availability: AvailabilityResult | None = None


# ============================================================
# Helpers internos
# ============================================================


def _formato_fecha_humana(dt: datetime) -> str:
    """Formato breve para mostrar al papá ('lunes 26 de mayo, 10:00')."""
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return (
        f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]}, "
        f"{dt.hour:02d}:{dt.minute:02d}"
    )


def _formatear_alternativas(alts: list[datetime]) -> str:
    return "; ".join(_formato_fecha_humana(a) for a in alts) or "(ninguna cercana)"


def _nivel_para_leads(estado: EstadoConversacion) -> str | None:
    """Mapea el nivel del estado al enum lead_nivel.

    NivelEducativo (maternal/kinder/primaria/secundaria) ya coincide con
    los valores válidos. Si el papá habla de prepa, también es válido.
    """
    capt = estado.estado_capturado
    nivel = capt.nivel_buscado_actual
    if nivel is None and capt.hijos:
        nivel = capt.hijos[0].nivel
    if nivel is None:
        return None
    return nivel.value


def _primer_hijo(estado: EstadoConversacion) -> tuple[str | None, int | None]:
    """Devuelve (nombre_hijo, edad_hijo) del primer hijo registrado, o (None, None)."""
    capt = estado.estado_capturado
    if not capt.hijos:
        return None, None
    h = capt.hijos[0]
    return h.nombre, h.edad


# ============================================================
# Ensure lead — crea si tenemos nombre, si no devuelve None
# ============================================================


async def _ensure_lead_para_cita(
    estado: EstadoConversacion, *, settings: Settings
) -> int | None:
    """Obtiene o crea el lead vinculado a esta sesión.

    Si el lead no existe pero tenemos `nombre_papa`, lo crea. Si no
    tenemos `nombre_papa`, NO crea — el handler entonces le pide a Sofía
    que pregunte el nombre antes de cerrar la cita.
    """
    existing = await get_lead_by_session(estado.session_id, settings=settings)
    if existing:
        # Actualiza datos que faltaran (best effort)
        nombre_hijo, edad_hijo = _primer_hijo(estado)
        updates: dict = {}
        if existing.child_name is None and nombre_hijo:
            updates["child_name"] = nombre_hijo
        if existing.child_age is None and edad_hijo is not None:
            updates["child_age"] = edad_hijo
        nivel = _nivel_para_leads(estado)
        if existing.nivel is None and nivel:
            updates["nivel"] = nivel
        if existing.parent_phone is None and estado.estado_capturado.telefono:
            updates["parent_phone"] = estado.estado_capturado.telefono
        if updates:
            await update_lead(existing.id, updates, settings=settings)
        return existing.id

    parent_name = estado.estado_capturado.nombre_papa
    if not parent_name:
        return None

    nombre_hijo, edad_hijo = _primer_hijo(estado)
    return await create_lead(
        parent_name=parent_name,
        channel=estado.canal.value,
        conversation_session_id=estado.session_id,
        parent_phone=estado.estado_capturado.telefono,
        child_name=nombre_hijo,
        child_age=edad_hijo,
        nivel=_nivel_para_leads(estado),
        settings=settings,
    )


# ============================================================
# Handler principal
# ============================================================


async def handle_appointment_intent(
    mensaje: str,
    estado: EstadoConversacion,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> AppointmentHandlerResult:
    """Procesa el flujo de agendado cuando intent == QUIERE_AGENDAR.

    Llamado por el orchestrator antes de invocar al LLM. Retorna un
    AppointmentHandlerResult cuyo `hint_para_prompt` se inyecta al user
    message para que Sofía responda con su tono propio guiada por el
    estado real del agendado.
    """
    settings = settings or get_settings()

    # 1. Extraer fecha/hora
    appt_dt = await extract_datetime(mensaje, now=now)
    if not appt_dt.es_completo or not appt_dt.es_alta_confianza:
        return AppointmentHandlerResult(
            hint_para_prompt=(
                "[FLUJO AGENDADO — el papá quiere visitar pero NO especificó fecha "
                "y hora claras. Pregúntale en UNA oración breve qué día y hora le "
                "queda mejor. NO inventes una fecha.]"
            ),
            acciones=["extract_failed"],
            appointment_datetime=appt_dt,
        )

    fecha_dt = appt_dt.to_datetime()
    if fecha_dt is None:
        return AppointmentHandlerResult(
            hint_para_prompt=(
                "[FLUJO AGENDADO — no pude convertir la fecha del papá. Pídele que "
                "te confirme día y hora exactos.]"
            ),
            acciones=["parse_failed"],
            appointment_datetime=appt_dt,
        )

    # 2. Verificar disponibilidad
    avail = await is_slot_available(
        fecha_dt, duracion_minutos=60, settings=settings, now=now
    )
    fecha_humana = _formato_fecha_humana(fecha_dt)

    if not avail.available:
        alts_str = _formatear_alternativas(avail.alternativas)
        if avail.reason == "fecha_pasada":
            hint = (
                f"[FLUJO AGENDADO — la fecha que pidió el papá ({fecha_humana}) ya pasó. "
                f"Pídele otra fecha próxima en UNA oración breve.]"
            )
        elif avail.reason == "dia_no_laborable":
            hint = (
                f"[FLUJO AGENDADO — ese día ({fecha_humana}) Lily no atiende. "
                f"Propón estas alternativas SIN inventar nada más: {alts_str}. "
                f"Una sola pregunta breve: '¿te queda alguna de estas?']"
            )
        elif avail.reason == "fuera_de_horario":
            hint = (
                f"[FLUJO AGENDADO — la hora ({fecha_humana}) está fuera del horario "
                f"de Lily. Propón estas alternativas: {alts_str}. Una sola pregunta.]"
            )
        elif avail.reason == "slot_ocupado":
            hint = (
                f"[FLUJO AGENDADO — ese horario ({fecha_humana}) ya está ocupado. "
                f"Propón estas alternativas: {alts_str}. Una sola pregunta breve.]"
            )
        else:  # supabase_error
            hint = (
                "[FLUJO AGENDADO — no pude verificar disponibilidad ahora. "
                "Pídele al papá que te confirme la fecha y dile que en breve "
                "le respondes.]"
            )
        return AppointmentHandlerResult(
            hint_para_prompt=hint,
            acciones=[f"availability:{avail.reason}"],
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 3. Disponible — necesitamos lead para crear la cita
    lead_id = await _ensure_lead_para_cita(estado, settings=settings)
    if lead_id is None:
        return AppointmentHandlerResult(
            hint_para_prompt=(
                f"[FLUJO AGENDADO — la fecha ({fecha_humana}) está disponible, "
                "pero aún no sabemos el nombre del papá. Pregúntaselo amable "
                "en UNA oración antes de cerrar la cita: '¿cómo te llamas?']"
            ),
            acciones=["missing_parent_name"],
            appointment_datetime=appt_dt,
            availability=avail,
        )

    # 4. Crear la cita en pendiente
    appointment_id = await create_appointment(
        lead_id=lead_id,
        fecha_hora=fecha_dt,
        duracion_min=60,
        notas=f"Solicitada por Sofía vía {estado.canal.value}. Mensaje del papá: {mensaje[:200]}",
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

    # 5. Auditoría: emit_event + avanzar stage
    acciones: list[str] = ["appointment_created"]
    await emit_event(
        "sofia_appointment_scheduled",
        lead_id=lead_id,
        session_id=estado.session_id,
        description=f"Sofía solicitó cita para {fecha_humana} (pendiente de aprobación)",
        metadata={
            "appointment_id": appointment_id,
            "fecha_hora": fecha_dt.isoformat(),
            "canal": estado.canal.value,
            "status": "pendiente",
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

    # 6. Email stub a Lily
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
        # Email queda solo en logs; Maple Platform mostrará la notificación
        # vía activity_events
        await send_email("", subject, body, settings=settings)
        acciones.append("email_skipped_no_recipient")

    # 7. Hint final para que Sofía responda al papá
    hint = (
        f"[FLUJO AGENDADO — la cita quedó REGISTRADA como PENDIENTE de aprobación "
        f"para {fecha_humana}. Tu respuesta DEBE: "
        f"1) Confirmar que registraste su solicitud (NO digas que está 'confirmada'). "
        f"2) Decir que en breve le avisamos por este mismo canal ({estado.canal.value}). "
        f"3) Ser cálida y breve, 2-3 oraciones. "
        f"NO inventes que ya está aprobada. La aprueba Lily desde la plataforma.]"
    )
    return AppointmentHandlerResult(
        hint_para_prompt=hint,
        acciones=acciones,
        lead_id=lead_id,
        appointment_id=appointment_id,
        appointment_datetime=appt_dt,
        availability=avail,
    )
