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


# ============================================================
# 5. PASO 1 — CIERRE FRAGMENTADO COMPLETO: la cita se CREA y persiste
#    aunque la fecha y los datos lleguen en turnos distintos.
# ============================================================


@pytest.mark.asyncio
async def test_cierre_fragmentado_crea_y_persiste_cita() -> None:
    """El papá da fecha en un turno, datos en otros. La fase pegajosa mantiene
    'agendando', los slots de fecha persisten, y el CÓDIGO cierra creando el
    appointment cuando todo está completo — sin depender de que Haiku improvise
    ni de que el intent dispare turno a turno."""
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import FaseAgendado
    from app.tools.campus import CampusResult

    # Guion por mensaje: (intent, extracción del state_extractor, (fecha,hora,conf))
    SCRIPT = {
        "Quiero agendar una visita": (
            Intent.QUIERE_AGENDAR,
            ExtraccionTurno(quiere_agendar=True, nivel_buscado="kinder"),
            (None, None, 0.0),
        ),
        "El lunes a las 10am": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(),
            ("2026-06-01", "10:00", 0.95),
        ),
        "Mi hijo Diego, 5 años, va en kinder 3": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(
                nombre_hijo="Diego", edad_hijo=5, grado_hijo="3 kinder", nivel_buscado="kinder"
            ),
            (None, None, 0.0),  # ← este turno NO trae fecha; debe usar el slot previo
        ),
        "Soy Oscar, mi correo oscar@x.com y mi cel 8441234567": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(
                nombre_papa="Oscar", email_papa="oscar@x.com", telefono="8441234567"
            ),
            (None, None, 0.0),  # ← tampoco trae fecha; el slot persiste desde turno 2
        ),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="test")

    campus1 = CampusResult(
        id=1, nombre="Campus 1", direccion="José Figueroa Siller 156", colonia="Doctores",
        ciudad="Saltillo", estado="Coahuila", niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )

    repo = _StatefulRepo()
    anthropic = _fake_anthropic(["(respuesta de Sofía, será sustituida en el cierre)"])
    create_appt = AsyncMock(return_value=123)

    # Patches constantes (orchestrator + hojas de appointment_flow) abiertos
    # durante toda la conversación.
    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(return_value=types.SimpleNamespace(
                available=True, reason=None, alternativas=[])),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=42)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        result = None
        for mensaje in SCRIPT:
            result = await procesar_turno(mensaje=mensaje, session_id="whatsapp:e2e", canal=None)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # Slots de fecha persistieron desde el turno 2 hasta el cierre
    assert capt.cita_fecha_slot == "2026-06-01"
    assert capt.cita_hora_slot == "10:00"
    # El CÓDIGO creó la cita exactamente una vez
    create_appt.assert_awaited_once()
    # Fase pegajosa cerró + estado agendado
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert repo._conv.agendado is True
    assert capt.campus_cita == "Campus 1"
    # La respuesta final es la plantilla determinística D.4 (no la de Haiku)
    assert "ya quedó agendada" in result.response
    assert "Campus 1" in result.response
    assert "1 de junio" in result.response
    assert "https://www.google.com/maps" in result.response


@pytest.mark.asyncio
async def test_fase_agendado_es_pegajosa_no_baja_sola() -> None:
    """Una vez en AGENDANDO, un turno sin señal temporal NO regresa a
    EXPLORANDO (sticky): el pipeline sigue corriendo."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:y", "assistant", "¿Qué día te queda?")

    anthropic = _fake_anthropic(["ok"])
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(hint_para_prompt="[FLUJO AGENDADO]", appointment_id=None)
    )

    from app.core.orchestrator import procesar_turno

    # Turno 1: señal de agendar → AGENDANDO
    ctx = _patches(
        repo, anthropic,
        classify=AsyncMock(return_value=_intent(Intent.QUIERE_AGENDAR)),
        extract=AsyncMock(return_value=ExtraccionTurno()), handler=handler,
    )
    _enter(ctx)
    try:
        await procesar_turno(mensaje="quiero agendar", session_id="whatsapp:y")
    finally:
        _exit(ctx)
    from app.core.state import FaseAgendado
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO

    # Turno 2: mensaje SIN señal temporal ni intent de agendar → sigue AGENDANDO
    handler.reset_mock()
    ctx = _patches(
        repo, anthropic,
        classify=AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO)),
        extract=AsyncMock(return_value=ExtraccionTurno()), handler=handler,
    )
    _enter(ctx)
    try:
        await procesar_turno(mensaje="ah ok perfecto gracias", session_id="whatsapp:y")
    finally:
        _exit(ctx)

    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO
    handler.assert_awaited()  # el pipeline siguió corriendo pese a no haber señal


# ============================================================
# 6. REPRODUCCIÓN de la prueba real de Oscar (2026-06-01):
#    fecha y hora en mensajes SEPARADOS, "2 kinder", y SIN nombre del niño.
#    Debe: (a) llenar la hora suelta, (b) NO cerrar sin el nombre del niño,
#    (c) crear el appointment cuando el nombre llega.
# ============================================================


@pytest.mark.asyncio
async def test_reproduccion_oscar_hora_suelta_y_nombre_obligatorio() -> None:
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import FaseAgendado
    from app.tools.campus import CampusResult

    # (intent, extracción, (fecha,hora,conf) que devuelve el extractor LLM de fecha)
    SCRIPT = {
        "hola, busco kinder para mi hijo de 4": (
            Intent.SALUDO_INICIAL,
            ExtraccionTurno(nivel_buscado="kinder", edad_hijo=4),
            (None, None, 0.0),
        ),
        "quiero agendar una visita": (
            Intent.QUIERE_AGENDAR, ExtraccionTurno(quiere_agendar=True), (None, None, 0.0),
        ),
        "Oscar Rodriguez, ing2oscar@gmail.com, +17866035862": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(
                nombre_papa="Oscar Rodriguez", email_papa="ing2oscar@gmail.com",
                telefono="+17866035862",
            ),
            (None, None, 0.0),
        ),
        "el jueves": (
            Intent.CONFUSO_OTRO, ExtraccionTurno(), ("2026-06-04", None, 0.95),
        ),
        # ↓ hora SOLA: el extractor LLM la devuelve vacía; el fallback determinístico la resuelve
        "2pm": (Intent.CONFUSO_OTRO, ExtraccionTurno(), (None, None, 0.0)),
        # ↓ grado que el LLM antes dejaba en None (ya viene normalizado simulando el fix)
        "2 kinder": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(grado_hijo="2° de Kinder", nivel_buscado="kinder"),
            (None, None, 0.0),
        ),
        # ↓ recién aquí el papá da el nombre del niño → cierre
        "se llama Diego": (
            Intent.CONFUSO_OTRO, ExtraccionTurno(nombre_hijo="Diego"), (None, None, 0.0),
        ),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="test")

    campus1 = CampusResult(
        id=1, nombre="Campus 1", direccion="José Figueroa Siller 156", colonia="Doctores",
        ciudad="Saltillo", estado="Coahuila", niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )
    repo = _StatefulRepo()
    anthropic = _fake_anthropic(["(respuesta de Sofía)"])
    create_appt = AsyncMock(return_value=123)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        # extract_datetime mockeado; extraer_hora_simple es REAL (prueba el fix de hora)
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(return_value=types.SimpleNamespace(
                available=True, reason=None, alternativas=[])),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=42)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        result = None
        for mensaje in SCRIPT:
            result = await procesar_turno(mensaje=mensaje, session_id="whatsapp:oscar", canal=None)
            capt = repo._conv.estado_capturado
            if mensaje == "2pm":
                # FIX 1: la hora suelta SÍ se guardó aunque la fecha vino antes
                assert capt.cita_hora_slot == "14:00", "la hora suelta no se guardó"
            if mensaje == "2 kinder":
                # FIX 3: con grado pero SIN nombre del niño, NO debe cerrar todavía
                assert capt.hijos and capt.hijos[0].grado == "2° de Kinder"
                assert create_appt.await_count == 0, "cerró sin el nombre del niño"
                assert capt.fase_agendado == FaseAgendado.AGENDANDO
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # El cierre ocurrió SOLO tras dar el nombre del niño
    create_appt.assert_awaited_once()
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert repo._conv.agendado is True
    assert capt.cita_fecha_slot == "2026-06-04" and capt.cita_hora_slot == "14:00"
    assert capt.hijos[0].nombre == "Diego"
    assert capt.hijos[0].grado == "2° de Kinder"
    # Mensaje final = plantilla D.4 con campus real + Maps
    assert "ya quedó agendada" in result.response
    assert "Campus 1" in result.response
    assert "4 de junio" in result.response
    assert "https://www.google.com/maps" in result.response
