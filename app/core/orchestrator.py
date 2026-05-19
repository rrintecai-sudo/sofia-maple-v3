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
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.intent_classifier import Intent, classify_intent
from app.core.learning_mode import guardar_feedback
from app.core.prompt_builder import build_system_blocks
from app.core.repository import get_repository
from app.core.state import (
    Canal,
    EstadoConversacion,
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

    # 3. Extraer estado y clasificar intención en paralelo (auxiliares baratos)
    extraccion_task = asyncio.create_task(extraer_de_mensaje(mensaje, estado.estado_capturado))
    intent_task = asyncio.create_task(classify_intent(mensaje))
    extraccion, intent_result = await asyncio.gather(extraccion_task, intent_task)

    # 4. Aplicar extracción al estado
    estado.estado_capturado = aplicar_extraccion(estado.estado_capturado, extraccion)

    # 5. Decidir fase del journey
    estado.fase_journey = _decidir_fase(estado, intent_result.intent, es_nueva)

    # 5bis. Pre-fetch tools cuando el intent lo amerita.
    # Por ahora solo campus — más tools (precios, horarios) se enganchan en
    # iteraciones siguientes. Inyectamos resultado al prompt como contexto.
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

    # 6. Componer prompt
    system_blocks = build_system_blocks(estado)

    # 7. Recuperar historial reciente (últimos 15 turnos)
    historial = await repo.list_recent_messages(session_id, limit=20)
    messages_llm = [
        {"role": _normalize_role(m["role"]), "content": m["content"]} for m in historial
    ]

    # 7ter. Si llamamos tools, inyectamos su resultado al mensaje del usuario
    # como hint para que el LLM lo use en la respuesta.
    mensaje_para_llm = mensaje
    if tools_data:
        tool_hint_lines = ["[Información traída de tools al momento:]"]
        for tool_name, data in tools_data.items():
            tool_hint_lines.append(f"- {tool_name}: {data}")
        mensaje_para_llm = f"{mensaje_para_llm}\n\n" + "\n".join(tool_hint_lines)

    messages_llm.append({"role": "user", "content": mensaje_para_llm})

    # 8. Persistir mensaje del usuario (antes de la llamada LLM)
    await _persist_user_message(repo, estado, mensaje)

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

        final_report = run_all_validators(
            respuesta=response_text,
            estado=estado.estado_capturado,
            intent=intent_result.intent,
            tools_called=[],  # Bloque 4 introducirá tools reales
            frases_usadas=estado.frases_usadas,
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
        llm_response=response_text,
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
    )


# ============================================================
# Helpers
# ============================================================


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
