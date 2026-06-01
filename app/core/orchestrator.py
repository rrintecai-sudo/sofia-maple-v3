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
from decimal import Decimal
from typing import Any

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.appointment_extractor import contiene_expresion_temporal
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
)
from app.core.learning_mode import guardar_feedback
from app.core.prompt_builder import build_system_blocks
from app.core.repository import get_repository
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
from app.tools.niveles import consultar_edades_de_nivel

log = logging.getLogger(__name__)

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


async def procesar_turno(
    mensaje: str,
    session_id: str,
    *,
    canal: Canal | None = None,
    tester: bool = False,
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
    extraccion_task = asyncio.create_task(extraer_de_mensaje(mensaje, estado.estado_capturado))
    intent_task = asyncio.create_task(
        classify_intent(
            mensaje,
            historial_reciente=historial_para_classifier,
            hay_turno_previo_assistant=hay_turno_previo_assistant,
        )
    )
    extraccion, intent_result = await asyncio.gather(extraccion_task, intent_task)

    # 4. Aplicar extracción al estado
    estado.estado_capturado = aplicar_extraccion(estado.estado_capturado, extraccion)

    # 4bis. PASO 1 (2026-05-29) — máquina PEGAJOSA de agendado controlada por
    # CÓDIGO (no por Haiku ni por el clasificador turno a turno). Se entra a
    # AGENDANDO con la PRIMERA señal (intent QUIERE_AGENDAR o expresión temporal)
    # y NO se reevalúa a la baja: el código colecta los 6 datos + día/hora hasta
    # cerrar. Persiste en sofia_conversations.estado_capturado (JSONB).
    capt = estado.estado_capturado
    senal_agendado = (
        intent_result.intent == Intent.QUIERE_AGENDAR
        or contiene_expresion_temporal(mensaje)
    )
    if capt.fase_agendado == FaseAgendado.EXPLORANDO and senal_agendado:
        capt.fase_agendado = FaseAgendado.AGENDANDO
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

    # 5quater. Handler de QUIERE_AGENDAR (Bloque C.1). Si el papá quiere
    # agendar, intentamos extraer fecha/hora, verificar disponibilidad y
    # (si todo cuadra) crear la cita en pendiente + notificar Lily. El
    # resultado se inyecta como hint al user message del LLM para que Sofía
    # responda con su tono.
    # PASO 1 (2026-05-29): mientras la fase pegajosa esté en AGENDANDO, el handler
    # determinístico corre CADA turno (no solo cuando el intent dispara). Así
    # colecta los slots de día/hora + 6 datos de forma fragmentada y cierra solo
    # cuando los tiene TODOS — el cierre lo decide el código, no Haiku.
    appointment_handler: AppointmentHandlerResult | None = None
    if en_agendado:
        try:
            appointment_handler = await handle_appointment_intent(mensaje, estado)
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
    ultimo_assistant_msg: str | None = None
    for m in reversed(historial):
        role = (m.get("role") or "").lower()
        if role in ("assistant", "ai"):
            ultimo_assistant_msg = m.get("content")
            break

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
    if intent_result.intent == Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO and ultimo_assistant_msg:
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
        tool_hint_lines = ["[Información traída de tools al momento:]"]
        for tool_name, data in tools_data.items():
            tool_hint_lines.append(f"- {tool_name}: {data}")
        mensaje_para_llm = f"{mensaje_para_llm}\n\n" + "\n".join(tool_hint_lines)

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

    # FIX 2/3 (2026-05-29): ¿hay una cita REALMENTE registrada (este turno o en
    # turnos previos)? Solo entonces Sofía puede confirmarla. Si no, el validator
    # `no_confirma_cita_inexistente` (severity=error) bloquea confirmaciones
    # fantasma ("registré tu solicitud", "Lily te comparte la dirección") y fuerza
    # a regenerar pidiendo los datos faltantes.
    cita_realmente_registrada = bool(
        (appointment_handler is not None and appointment_handler.appointment_id is not None)
        or estado.estado_capturado.cita_agendada
    )

    # 9. Llamar a Anthropic con loop de validación + regeneración
    anthropic = get_anthropic()
    max_regen = settings.max_regenerations_per_turn if settings.enable_validators else 0
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

    llm_started = time.perf_counter()
    for intento in range(max_regen + 1):
        try:
            message = await anthropic.chat(
                system_blocks=system_blocks,
                messages=messages_llm + extra_messages,
                model=settings.anthropic_model_principal,
                max_tokens=600,
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
                        appointment_handler.campus
                        and appointment_handler.campus.google_maps_url
                    ),
                },
            )

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
