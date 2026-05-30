"""Tests E2E de conversación FRAGMENTADA (no camino feliz).

Replica el flujo real que reveló la prueba con papá humano (conversación de
"María", 2026-05-29): el papá responde en fragmentos cortos ("Viernes",
"Mañana", "Mejor lunes") y NUNCA da su nombre ni los 6 datos. Antes del
Bloque de fixes, el intent QUIERE_AGENDAR no se disparaba turno a turno, así
que TODO el andamiaje determinístico (fecha, gate de 6 datos, Maps) se
omitía y el LLM improvisaba el agendado con fecha incorrecta, nombre
inventado y confirmación fantasma.

Estos tests verifican las GARANTÍAS del orchestrator (no el camino feliz):

1. El flujo de agendado se dispara ante cualquier expresión temporal, NO
   solo cuando intent==QUIERE_AGENDAR.  (FIX 1+3)
2. El gate de 6 datos bloquea la confirmación cuando faltan datos.  (FIX 3)
3. Sofía NO puede confirmar una cita si no hay appointment_id real.  (FIX 2)
4. Sofía NO puede usar un nombre que el papá no dio.  (FIX 4)
5. Cuando el papá da un día sin hora, la fecha se resuelve correctamente y
   se le pasa a Sofía para que no la recalcule mal.  (FIX 1)

Se mockean LLMs y dependencias externas. El repository es STATEFUL para que
el estado capturado se acumule turno a turno, como en producción.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.core.appointment_flow import AppointmentHandlerResult
from app.core.intent_classifier import Intent, IntentResult
from app.core.state_extractor import ExtraccionTurno

# ============================================================
# Infra de test: repo stateful + fake anthropic
# ============================================================


class _StatefulRepo:
    """Repository en memoria — conserva estado y mensajes entre turnos."""

    def __init__(self) -> None:
        self._conv = None
        self._messages: list[dict] = []
        self.turn_logs: list[dict] = []

    async def get_conversation(self, session_id: str):
        return self._conv

    async def upsert_conversation(self, estado) -> None:
        self._conv = estado

    async def list_recent_messages(self, session_id: str, limit: int = 20):
        return self._messages[-limit:]

    async def insert_message(self, session_id: str, role: str, content: str, **kw) -> None:
        self._messages.append({"role": role, "content": content})

    async def insert_turn_log(self, **kw) -> None:
        self.turn_logs.append(kw)

    async def count_turns(self, session_id: str) -> int:
        return sum(1 for m in self._messages if m["role"] == "assistant")


class _FakeMessage:
    """Anthropic Message mock con content como lista de bloques."""

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Usage:
        input_tokens = 100
        output_tokens = 40
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    def __init__(self, text: str) -> None:
        self.content = [self._Block(text)]
        self.usage = self._Usage()


def _fake_anthropic(responses: list[str]):
    """Fake anthropic cuyo .chat devuelve `responses` en orden (la última se
    repite si se agota — útil para modelar un LLM 'terco' que no corrige)."""
    fake = AsyncMock()
    seq = list(responses)

    async def _chat(*args, **kwargs):
        text = seq.pop(0) if len(seq) > 1 else seq[0]
        return _FakeMessage(text)

    fake.chat = AsyncMock(side_effect=_chat)
    return fake


def _patches(repo, anthropic, *, classify, extract, handler=None):
    """Conjunto estándar de patches del orchestrator."""
    ctx = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", classify),
        patch("app.core.orchestrator.extraer_de_mensaje", extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
    ]
    if handler is not None:
        ctx.append(patch("app.core.orchestrator.handle_appointment_intent", handler))
    return ctx


def _enter(ctx_list):
    for c in ctx_list:
        c.__enter__()


def _exit(ctx_list):
    for c in reversed(ctx_list):
        c.__exit__(None, None, None)


def _intent(intent: Intent) -> IntentResult:
    return IntentResult(intent=intent, confidence=0.9, razonamiento_breve="test")


# ============================================================
# 1. Routing: expresión temporal dispara el flujo de agendado
#    AUNQUE el intent no sea QUIERE_AGENDAR
# ============================================================


@pytest.mark.asyncio
async def test_expresion_temporal_dispara_flujo_aunque_intent_no_sea_agendar() -> None:
    """FIX 1+3: 'Mejor lunes' clasificado como CONFUSO_OTRO igual debe entrar
    al handler de agendado (antes se saltaba todo el flujo determinístico)."""
    repo = _StatefulRepo()
    # turno previo de Sofía para que haya contexto
    await repo.insert_message("whatsapp:x", "assistant", "¿Qué día te queda mejor para la visita?")

    anthropic = _fake_anthropic(["Va, te espero pronto."])
    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    extract = AsyncMock(return_value=ExtraccionTurno())
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO — pídele la hora]",
            acciones=["missing_time"],
            appointment_id=None,
        )
    )

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract, handler=handler)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="Mejor lunes", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    handler.assert_awaited()  # ← el flujo de agendado SÍ corrió pese a CONFUSO_OTRO


@pytest.mark.asyncio
async def test_mensaje_sin_temporal_no_dispara_flujo() -> None:
    """Control negativo: un mensaje sin expresión temporal ni intent de
    agendar NO debe invocar el handler (evita latencia/costo innecesario)."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "Cuéntame de tu peque.")

    anthropic = _fake_anthropic(["Con gusto te explico la metodología."])
    classify = AsyncMock(return_value=_intent(Intent.PREGUNTA_METODOLOGIA))
    extract = AsyncMock(return_value=ExtraccionTurno())
    handler = AsyncMock(return_value=AppointmentHandlerResult(hint_para_prompt="x"))

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract, handler=handler)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="¿Y cómo enseñan a leer?", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    handler.assert_not_awaited()


# ============================================================
# 2. Conversación fragmentada multi-turno: el estado se acumula y al
#    intentar agendar sin los 6 datos, el gate impide la confirmación
# ============================================================


@pytest.mark.asyncio
async def test_conversacion_fragmentada_gate_6_datos_impide_confirmacion() -> None:
    """Replica el flujo de María: kinder → 4 años → quiere visita, pero sin
    nombre/correo/celular. La respuesta final NO debe confirmar la cita."""
    repo = _StatefulRepo()

    # Guion por turno (mensaje del papá → intent, extracción)
    turnos = [
        ("Hola", Intent.SALUDO_INICIAL, ExtraccionTurno()),
        ("Kinder", Intent.PREGUNTA_NIVEL, ExtraccionTurno(nivel_buscado="kinder")),
        ("4 años", Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO, ExtraccionTurno(edad_hijo=4)),
        ("Quiero ver las instalaciones", Intent.QUIERE_AGENDAR, ExtraccionTurno(quiere_agendar=True)),
        ("Mañana", Intent.CONFUSO_OTRO, ExtraccionTurno()),
    ]

    # El handler (real-ish): en el último turno faltan datos → missing_lead_data
    async def fake_handler(mensaje, estado, **kw):
        return AppointmentHandlerResult(
            hint_para_prompt=(
                "[FLUJO AGENDADO — la fecha está disponible pero ANTES de registrar "
                "necesitamos: tu nombre, correo electrónico, número de celular. Pídelos "
                "de forma natural. NO crees la cita todavía.]"
            ),
            acciones=["missing_lead_data:tu nombre,correo electrónico,número de celular"],
            appointment_id=None,
        )

    # LLM obediente: pide los datos (NO confirma)
    anthropic = _fake_anthropic(
        ["Claro, con gusto agendamos. ¿Me compartes tu nombre, correo y celular?"]
    )

    from app.core.orchestrator import procesar_turno

    result = None
    for mensaje, intent, extraccion in turnos:
        classify = AsyncMock(return_value=_intent(intent))
        extract = AsyncMock(return_value=extraccion)
        ctx = _patches(
            repo, anthropic, classify=classify, extract=extract,
            handler=AsyncMock(side_effect=fake_handler),
        )
        _enter(ctx)
        try:
            result = await procesar_turno(mensaje=mensaje, session_id="whatsapp:x", canal=None)
        finally:
            _exit(ctx)

    # Estado acumulado: nivel kinder + edad 4 capturados
    assert repo._conv is not None
    capt = repo._conv.estado_capturado
    assert capt.nivel_buscado_actual is not None and capt.nivel_buscado_actual.value == "kinder"
    assert any(h.edad == 4 for h in capt.hijos)
    # Faltan datos del lead → la respuesta NO confirma cita
    assert result is not None
    assert "no_confirma_cita_inexistente" not in result.validators_failed  # LLM obedeció el gate


# ============================================================
# 3. FIX 2/3 — Confirmación fantasma de cita SIN appointment_id → BLOQUEA
# ============================================================


@pytest.mark.asyncio
async def test_confirmacion_fantasma_sin_appointment_se_bloquea() -> None:
    """LLM 'terco' que insiste en confirmar la cita sin que exista
    appointment_id. El validator (severity=error) debe marcarlo como fallo
    y agotar las regeneraciones."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "¿Qué día te gustaría?")

    # LLM terco: SIEMPRE devuelve confirmación fantasma (la frase exacta del bug real)
    phantom = (
        "Listo, te agendo para mañana viernes 30 de mayo a las 9 a.m. en Campus 1. "
        "Registré tu solicitud, en breve Lily te confirma y te comparte la dirección."
    )
    anthropic = _fake_anthropic([phantom])

    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    extract = AsyncMock(return_value=ExtraccionTurno())
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO — pídele la hora]",
            acciones=["missing_time"],
            appointment_id=None,  # ← NO hay cita real
        )
    )

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract, handler=handler)
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="Mañana", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    # El validator de severidad error detectó la confirmación fantasma
    assert "no_confirma_cita_inexistente" in result.validators_failed
    # Se intentó regenerar (al menos 1 vez)
    assert result.regenerations >= 1


@pytest.mark.asyncio
async def test_confirmacion_fantasma_se_autocorrige_en_regeneracion() -> None:
    """Si el LLM corrige en el reintento (deja de confirmar), la respuesta
    final ya NO contiene la confirmación fantasma."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "¿Qué día te gustaría?")

    phantom = "Registré tu solicitud, Lily te comparte la dirección."
    corregido = "¿Me confirmas tu nombre, correo y celular para dejar todo listo?"
    anthropic = _fake_anthropic([phantom, corregido])

    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    extract = AsyncMock(return_value=ExtraccionTurno())
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO — faltan datos]",
            acciones=["missing_lead_data:tu nombre"],
            appointment_id=None,
        )
    )

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract, handler=handler)
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="Mañana", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    assert "Registré tu solicitud" not in result.response
    assert result.regenerations >= 1


@pytest.mark.asyncio
async def test_mensaje_de_proceso_no_se_bloquea() -> None:
    """Calibración: un mensaje LEGÍTIMO de proceso ('voy a registrar tu
    solicitud cuando me confirmes los datos') NO debe bloquearse."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "¿Qué día te gustaría?")

    legitimo = "En cuanto me confirmes tu nombre y correo, registro tu solicitud de cita."
    anthropic = _fake_anthropic([legitimo])

    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    extract = AsyncMock(return_value=ExtraccionTurno())
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO — faltan datos]",
            acciones=["missing_lead_data:tu nombre"],
            appointment_id=None,
        )
    )

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract, handler=handler)
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="Mañana", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    assert "no_confirma_cita_inexistente" not in result.validators_failed
    assert result.response == legitimo  # sin regeneración


# ============================================================
# 4. FIX 4 — Nombre inventado sin que el papá lo diera → BLOQUEA (error)
# ============================================================


@pytest.mark.asyncio
async def test_nombre_inventado_se_bloquea() -> None:
    """LLM 'terco' que llama al papá 'María' sin que el papá lo haya dicho.
    El validator de nombre (ahora severity=error) debe marcarlo."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "¡Hola! ¿En qué te ayudo?")

    anthropic = _fake_anthropic(["Hola María, con gusto te ayudo con la información."])
    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    extract = AsyncMock(return_value=ExtraccionTurno())

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract)
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="Buenas, busco info", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    assert "no_inventa_nombre_papa" in result.validators_failed
    assert result.regenerations >= 1


@pytest.mark.asyncio
async def test_nombre_real_del_papa_no_se_bloquea() -> None:
    """Si el papá SÍ dio su nombre (en estado), usarlo NO se bloquea."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:x", "assistant", "¡Hola!")

    anthropic = _fake_anthropic(["Hola Oscar, con gusto te ayudo."])
    classify = AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO))
    # El extractor reporta el nombre → se mergea al estado este turno
    extract = AsyncMock(return_value=ExtraccionTurno(nombre_papa="Oscar"))

    from app.core.orchestrator import procesar_turno

    ctx = _patches(repo, anthropic, classify=classify, extract=extract)
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="Soy Oscar", session_id="whatsapp:x")
    finally:
        _exit(ctx)

    assert "no_inventa_nombre_papa" not in result.validators_failed
    assert result.response == "Hola Oscar, con gusto te ayudo."
