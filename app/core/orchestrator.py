"""Orchestrator MVP — procesa un turno de conversación.

Flujo (Block 2, sin validators todavía):
1. Cargar/crear EstadoConversacion.
2. Extraer estado del mensaje del usuario (state_extractor).
3. Clasificar intención (intent_classifier).
4. Mapear intención → posible cambio de fase del journey.
5. Componer system prompt (prompt_builder).
6. Llamar a Claude Haiku 4.5 con caching y memoria reciente.
7. Persistir: mensajes (user + assistant), turn_log, estado actualizado.

NO incluye validators (Bloque 3) ni tools custom (Bloque 4).
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
from app.core.prompt_builder import build_system_blocks
from app.core.repository import get_repository
from app.core.state import (
    Canal,
    EstadoConversacion,
    FaseJourney,
    Modo,
)
from app.core.state_extractor import aplicar_extraccion, extraer_de_mensaje
from app.observability.costs import calculate_cost

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

    # 3. Extraer estado y clasificar intención en paralelo (auxiliares baratos)
    extraccion_task = asyncio.create_task(extraer_de_mensaje(mensaje, estado.estado_capturado))
    intent_task = asyncio.create_task(classify_intent(mensaje))
    extraccion, intent_result = await asyncio.gather(extraccion_task, intent_task)

    # 4. Aplicar extracción al estado
    estado.estado_capturado = aplicar_extraccion(estado.estado_capturado, extraccion)

    # 5. Decidir fase del journey
    estado.fase_journey = _decidir_fase(estado, intent_result.intent, es_nueva)

    # 6. Componer prompt
    system_blocks = build_system_blocks(estado)

    # 7. Recuperar historial reciente (últimos 15 turnos)
    historial = await repo.list_recent_messages(session_id, limit=20)
    messages_llm = [
        {"role": _normalize_role(m["role"]), "content": m["content"]} for m in historial
    ]
    # Agregar el mensaje actual
    messages_llm.append({"role": "user", "content": mensaje})

    # 8. Persistir mensaje del usuario (antes de la llamada LLM)
    await _persist_user_message(repo, estado, mensaje)

    # 9. Llamar a Anthropic
    anthropic = get_anthropic()
    llm_started = time.perf_counter()
    try:
        message = await anthropic.chat(
            system_blocks=system_blocks,
            messages=messages_llm,
            model=settings.anthropic_model_principal,
            max_tokens=600,
            temperature=0.55,
        )
    except Exception as exc:
        log.error("anthropic chat failed", extra={"error": str(exc), "session_id": session_id})
        raise
    llm_latency = int((time.perf_counter() - llm_started) * 1000)

    # 10. Extraer texto y métricas
    response_text = _extract_text_response(message)
    usage = getattr(message, "usage", None)
    tokens_input = getattr(usage, "input_tokens", 0) or 0
    tokens_output = getattr(usage, "output_tokens", 0) or 0
    tokens_cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens_cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = calculate_cost(
        model=settings.anthropic_model_principal,
        input_tokens=tokens_input,
        output_tokens=tokens_output,
        cache_write_tokens=tokens_cache_write,
        cache_read_tokens=tokens_cache_read,
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
    await repo.insert_turn_log(
        session_id=session_id,
        turn_number=turn_number,
        user_message=mensaje,
        intent=intent_result.intent.value,
        prompt_compuesto=prompt_compuesto[:50000],  # cap por si acaso
        llm_response=response_text,
        final_response=response_text,
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
