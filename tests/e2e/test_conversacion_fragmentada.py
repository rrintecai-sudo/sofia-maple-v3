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

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from app.core.appointment_flow import AppointmentHandlerResult
from app.core.intent_classifier import Intent, IntentResult
from app.core.state_extractor import ExtraccionTurno

# Reloj FIJO para tests con resolución de fecha determinística (miércoles 3-jun
# 2026, 9:00 a.m., antes del cierre de Lily). Inyectado vía procesar_turno(now=).
# "mañana"→jue 4-jun; "el jueves"→4-jun; "el viernes"→5-jun; "hoy"→3-jun.
_NOW_MIE = datetime(2026, 6, 3, 9, 0, tzinfo=ZoneInfo("America/Monterrey"))

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
        (
            "Quiero ver las instalaciones",
            Intent.QUIERE_AGENDAR,
            ExtraccionTurno(quiere_agendar=True),
        ),
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
            repo,
            anthropic,
            classify=classify,
            extract=extract,
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
    # Intent sustantivo (pregunta general) → Haiku CORRE (un confuso_otro sin datos
    # ahora lo reorienta el código). Aquí queremos ejercitar el validator de nombre.
    classify = AsyncMock(return_value=_intent(Intent.PREGUNTA_GENERAL_MAPLE))
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
        # "Mañana" (no un día de semana) → el resolver determinístico no aplica,
        # así que la fecha la gobierna el extractor LLM mockeado. Verifica la
        # PERSISTENCIA del slot entre turnos, no la resolución determinística.
        "Mañana a las 10am": (
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
            ExtraccionTurno(nombre_papa="Oscar", email_papa="oscar@x.com", telefono="8441234567"),
            (None, None, 0.0),  # ← tampoco trae fecha; el slot persiste desde turno 2
        ),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual, **kw):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="test")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
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
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(available=True, reason=None, alternativas=[])
            ),
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
            result = await procesar_turno(
                mensaje=mensaje, session_id="whatsapp:e2e", canal=None, now=_NOW_MIE
            )
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # "Mañana" (now=mié 3-jun) → jue 4-jun, resuelto por extraer_fecha_relativa.
    assert capt.cita_fecha_slot == "2026-06-04"
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
    assert "4 de junio" in result.response
    assert "https://www.google.com/maps" in result.response


@pytest.mark.asyncio
async def test_fase_agendado_es_pegajosa_no_baja_sola() -> None:
    """Una vez en AGENDANDO, un turno sin señal temporal NO regresa a
    EXPLORANDO (sticky): el pipeline sigue corriendo."""
    repo = _StatefulRepo()
    await repo.insert_message("whatsapp:y", "assistant", "¿Qué día te queda?")

    anthropic = _fake_anthropic(["ok"])
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO]", appointment_id=None
        )
    )

    from app.core.orchestrator import procesar_turno

    # Turno 1: señal de agendar → AGENDANDO
    ctx = _patches(
        repo,
        anthropic,
        classify=AsyncMock(return_value=_intent(Intent.QUIERE_AGENDAR)),
        extract=AsyncMock(return_value=ExtraccionTurno()),
        handler=handler,
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
        repo,
        anthropic,
        classify=AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO)),
        extract=AsyncMock(return_value=ExtraccionTurno()),
        handler=handler,
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
            Intent.QUIERE_AGENDAR,
            ExtraccionTurno(quiere_agendar=True),
            (None, None, 0.0),
        ),
        "Oscar Rodriguez, ing2oscar@gmail.com, +17866035862": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(
                nombre_papa="Oscar Rodriguez",
                email_papa="ing2oscar@gmail.com",
                telefono="+17866035862",
            ),
            (None, None, 0.0),
        ),
        "el jueves": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(),
            ("2026-06-04", None, 0.95),
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
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(nombre_hijo="Diego"),
            (None, None, 0.0),
        ),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual, **kw):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="test")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
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
            AsyncMock(
                return_value=types.SimpleNamespace(available=True, reason=None, alternativas=[])
            ),
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
            result = await procesar_turno(
                mensaje=mensaje, session_id="whatsapp:oscar", canal=None, now=_NOW_MIE
            )
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


# ============================================================
# 7. ENTRADA SUCIA REAL (2026-06-01): typo de hora ("10a"), nombre+edad
#    juntos ("Jose, 4 años" con el LLM metiéndolo como papá), y la fecha/grado
#    rescatados por CONFIRMACIÓN ("si dale"). Usa el extractor REAL (openai
#    stubbeado) para ejercitar la corrección nombre-papá→hijo de verdad.
# ============================================================


@pytest.mark.asyncio
async def test_entrada_sucia_cierra_con_confirmacion() -> None:
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import FaseAgendado
    from app.tools.campus import CampusResult

    # intent + JSON CRUDO del extractor LLM (buggy a propósito) + fecha del extractor
    SCRIPT = {
        "hola quiero agendar": (
            Intent.QUIERE_AGENDAR,
            '{"quiere_agendar": true}',
            (None, None, 0.0),
        ),
        # ↓ el LLM mete "Jose" como nombre del PAPÁ (bug real) → (c) lo corrige a hijo
        "Jose, 4 anos": (
            Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
            '{"nombre_papa": "Jose", "edad_hijo": 4, "nivel_buscado": "kinder"}',
            (None, None, 0.0),
        ),
        # ↓ typo "10a": el extractor LLM de fecha falla; extraer_hora_simple lo rescata
        "viernes 10a,": (Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO, "{}", (None, None, 0.0)),
        # ↓ confirmación: rescata fecha "5 de junio" y grado "2° de Kinder" de la propuesta de Sofía
        "si dale": (Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO, "{}", (None, None, 0.0)),
        "Oscar Rodriguez, ing2oscar@gmail.com, +17866035862": (
            Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
            '{"nombre_papa": "Oscar Rodriguez", "email_papa": "ing2oscar@gmail.com", "telefono": "+17866035862"}',
            (None, None, 0.0),
        ),
    }
    # Respuestas de Sofía por turno. La de "viernes 10a," PROPONE fecha + grado
    # para que la confirmación siguiente los rescate.
    RESPUESTAS = [
        "¡Hola! ¿Me dices el nombre y la edad de tu peque?",
        "Perfecto, Jose. ¿Qué día y hora te queda mejor?",
        "Va, 10 de la mañana. ¿Confirmas el viernes 5 de junio, y que Jose va en 2° de Kinder?",
        "Genial. ¿Me compartes tu nombre, correo y celular?",
        "(se sustituye por la plantilla D.4)",
    ]

    class _StubOpenAI:
        def is_configured(self):
            return True

        async def classify(self, text, instructions, model=None):
            for msg, (_i, js, _dt) in SCRIPT.items():
                if msg in text:
                    return js
            return "{}"

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="t")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )
    repo = _StatefulRepo()
    anthropic = _fake_anthropic(RESPUESTAS)
    create_appt = AsyncMock(return_value=777)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        # extractor de estado REAL → ejercita (c); openai stubbeado
        patch("app.core.state_extractor.get_openai", return_value=_StubOpenAI()),
        # extractor de fecha mockeado (simula que el LLM falla los typos)
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(available=True, reason=None, alternativas=[])
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=99)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        result = None
        for mensaje in SCRIPT:
            result = await procesar_turno(
                mensaje=mensaje, session_id="whatsapp:sucia", canal=None, now=_NOW_MIE
            )
            capt = repo._conv.estado_capturado
            if mensaje == "viernes 10a,":
                assert capt.cita_hora_slot == "10:00", "el typo '10a' no se rescató"
            if mensaje == "si dale":
                # la confirmación rescató la fecha y el grado de la propuesta de Sofía
                assert capt.cita_fecha_slot == "2026-06-05", "no rescató la fecha propuesta"
                assert capt.hijos[0].grado == "2° de Kinder", "no rescató el grado propuesto"
                assert create_appt.await_count == 0  # aún falta nombre/correo/cel del papá
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # (c): "Jose" terminó como NIÑO, y "Oscar Rodriguez" como papá (no clavado)
    assert capt.hijos[0].nombre == "Jose"
    assert capt.nombre_papa == "Oscar Rodriguez"
    # cierre por código
    create_appt.assert_awaited_once()
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert repo._conv.agendado is True
    assert "ya quedó agendada" in result.response
    assert "5 de junio" in result.response
    assert "Campus 1" in result.response


# ============================================================
# 8. ESTADO PRE-CONTAMINADO (2026-06-01): sesión reusada con un hijo HUÉRFANO
#    y nombre_papa viejo CLAVADO ("Jose"). El cierre debe crear el appointment
#    con el niño y el papá correctos (FIX (d) + (e)). Caso real: en WhatsApp la
#    sesión es el teléfono y persiste — esto pasará con papás reales.
# ============================================================


@pytest.mark.asyncio
async def test_estado_contaminado_cierra_con_datos_correctos() -> None:
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import (
        Canal,
        EstadoCapturado,
        EstadoConversacion,
        FaseAgendado,
        FaseJourney,
        HijoInfo,
    )
    from app.tools.campus import CampusResult

    # Estado CONTAMINADO de un intento viejo: Jose clavado como papá, hijo huérfano
    # {edad:4}, fase ya AGENDANDO, slots de fecha/hora ya puestos, email/tel ya dados.
    contaminado = EstadoConversacion(
        session_id="web:dirty",
        canal=Canal.WEB,
        identificador="dirty",
        fase_journey=FaseJourney.AGENDADO,
        estado_capturado=EstadoCapturado(
            nombre_papa="Jose",  # ← clavado y MAL (era el niño del intento viejo)
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
            hijos=[HijoInfo(edad=4)],  # ← huérfano sin nombre/nivel/grado
            fase_agendado=FaseAgendado.AGENDANDO,
            cita_fecha_slot="2026-06-05",
            cita_hora_slot="11:00",
        ),
    )

    SCRIPT = {
        # presentación explícita → (e) corrige "Jose"→"Oscar"; nombre del niño Emanuel
        "Emanuel Rodriguez, yo soy Oscar Rodriguez": '{"nombre_hijo": "Emanuel", "nombre_papa": "Oscar Rodriguez"}',
        # grado + nivel del niño (crea/fusiona) → (d) consolida con el huérfano
        "Emanuel, 2° de Kinder": '{"nombre_hijo": "Emanuel", "grado_hijo": "2° de Kinder", "nivel_buscado": "kinder"}',
    }

    class _StubOpenAI:
        def is_configured(self):
            return True

        async def classify(self, text, instructions, model=None):
            for msg, js in SCRIPT.items():
                if msg in text:
                    return js
            return "{}"

    async def fake_extract_dt(mensaje, *, now=None):
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="t")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )
    repo = _StatefulRepo()
    repo._conv = contaminado  # ← arrancamos con el estado sucio
    anthropic = _fake_anthropic(["ok", "ok"])
    create_appt = AsyncMock(return_value=555)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch(
            "app.core.orchestrator.classify_intent",
            side_effect=lambda *a, **k: _intent(Intent.CONFUSO_OTRO),
        ),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.state_extractor.get_openai", return_value=_StubOpenAI()),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(available=True, reason=None, alternativas=[])
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        respuestas = []
        for mensaje in SCRIPT:
            r = await procesar_turno(mensaje=mensaje, session_id="web:dirty", canal=None)
            respuestas.append(r.response)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # (e): el nombre del papá se corrigió de "Jose" a "Oscar Rodriguez"
    assert capt.nombre_papa == "Oscar Rodriguez"
    # (d): el hijo consolidado tiene nombre+edad+grado pese al huérfano
    hijo = capt.hijo_efectivo()
    assert hijo.nombre == "Emanuel"
    assert hijo.edad == 4
    # FIX 1: con 4 años en Kinder, el grado se DEDUCE → "2° de Kinder"
    assert hijo.grado == "2° de Kinder"
    # cierre por código a pesar del estado contaminado (puede cerrar en cuanto
    # tiene todo, gracias a FIX 1 que deduce el grado de la edad)
    create_appt.assert_awaited_once()
    assert capt.fase_agendado == FaseAgendado.CERRADO
    # la plantilla D.4 salió en el turno del cierre
    assert any("ya quedó agendada" in r for r in respuestas)
    assert any("5 de junio" in r for r in respuestas)


# ============================================================
# 9. RE-ARMADO (2026-06-02): sesión que YA cerró una cita + "quiero agendar"
#    nuevo (otro hijo) → crea un SEGUNDO appointment real (grado deducido + D.4),
#    NO ghost-close. Y un temporal suelto NO reabre una cita legítima.
# ============================================================


def _seed_cerrado() -> EstadoConversacion:  # noqa: F821
    """Sesión ya CERRADA (cita de Emanuel) reusada — como WhatsApp por teléfono."""
    from app.core.state import (
        Canal,
        EstadoCapturado,
        EstadoConversacion,
        FaseAgendado,
        FaseJourney,
        HijoInfo,
        NivelEducativo,
    )

    return EstadoConversacion(
        session_id="web:reuse",
        canal=Canal.WEB,
        identificador="reuse",
        fase_journey=FaseJourney.POST_AGENDADO,
        agendado=True,
        estado_capturado=EstadoCapturado(
            nombre_papa="Oscar Rodriguez",
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
            hijos=[
                HijoInfo(
                    nombre="Emanuel", edad=4, nivel=NivelEducativo.KINDER, grado="2° de Kinder"
                )
            ],
            fase_agendado=FaseAgendado.CERRADO,
            cita_agendada=True,
            cita_fecha_slot="2026-06-05",
            cita_hora_slot="11:00",
        ),
    )


@pytest.mark.asyncio
async def test_rearmado_segunda_cita_se_crea_real() -> None:
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import FaseAgendado
    from app.tools.campus import CampusResult

    SCRIPT = {
        # intent explícito QUIERE_AGENDAR → re-arma (resetea Emanuel + cita vieja)
        "quiero agendar otra visita para mi otro hijo": (
            Intent.QUIERE_AGENDAR,
            ExtraccionTurno(quiere_agendar=True),
            (None, None, 0.0),
        ),
        # niño NUEVO (Pablo, 4) → grado se DEDUCE (4 → 2° de Kinder)
        "Pablo, 4 años": (
            Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
            ExtraccionTurno(nombre_hijo="Pablo", edad_hijo=4),
            (None, None, 0.0),
        ),
        "el viernes": (Intent.CONFUSO_OTRO, ExtraccionTurno(), ("2026-06-12", None, 0.95)),
        "a las 10am": (Intent.CONFUSO_OTRO, ExtraccionTurno(), (None, "10:00", 0.95)),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual, **kw):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h, c = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=c, razonamiento="t")

    async def fake_evaluar_dia(fecha_dia, *, duracion_min=60, settings=None, now=None):
        return types.SimpleNamespace(available=True, reason="ok", alternativas=[], resumen="")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )
    repo = _StatefulRepo()
    repo._conv = _seed_cerrado()
    anthropic = _fake_anthropic(["ok"])
    create_appt = AsyncMock(return_value=999)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch("app.core.appointment_flow.evaluar_dia", side_effect=fake_evaluar_dia),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        respuestas = []
        for i, mensaje in enumerate(SCRIPT):
            r = await procesar_turno(mensaje=mensaje, session_id="web:reuse", canal=None)
            respuestas.append(r.response)
            if i == 0:
                # tras el re-armado: AGENDANDO + estado viejo reseteado
                c = repo._conv.estado_capturado
                assert c.fase_agendado == FaseAgendado.AGENDANDO
                assert c.hijos == [] and c.cita_agendada is False
                assert c.cita_fecha_slot is None and c.cita_hora_slot is None
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # se creó un SEGUNDO appointment real (no ghost-close)
    create_appt.assert_awaited_once()
    assert capt.fase_agendado == FaseAgendado.CERRADO
    # el niño nuevo, con grado DEDUCIDO de la edad (4 → 2° de Kinder)
    assert capt.hijos[0].nombre == "Pablo"
    assert capt.hijos[0].grado == "2° de Kinder"
    # la plantilla D.4 (Maps) volvió sola al correr el pipeline
    assert any("ya quedó agendada" in r for r in respuestas)
    assert any("https://www.google.com/maps" in r for r in respuestas)


@pytest.mark.asyncio
async def test_temporal_suelto_no_reabre_cita_cerrada() -> None:
    """'nos vemos el viernes' (temporal, NO QUIERE_AGENDAR) NO reabre la cita."""
    from app.core.state import FaseAgendado

    repo = _StatefulRepo()
    repo._conv = _seed_cerrado()
    anthropic = _fake_anthropic(["¡Claro, ahí nos vemos!"])
    handler = AsyncMock()

    from app.core.orchestrator import procesar_turno

    ctx = _patches(
        repo,
        anthropic,
        classify=AsyncMock(return_value=_intent(Intent.CONFUSO_OTRO)),
        extract=AsyncMock(return_value=ExtraccionTurno()),
        handler=handler,
    )
    _enter(ctx)
    try:
        await procesar_turno(mensaje="nos vemos el viernes", session_id="web:reuse")
    finally:
        _exit(ctx)

    # NO se re-armó: sigue CERRADO y el pipeline NO corrió
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.CERRADO
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_ghost_close_bloqueado_tras_rearmar_sin_crear() -> None:
    """Tras re-armar, si el pipeline NO crea cita y Haiku dice 'quedó agendada',
    el validador (re-armado) lo bloquea."""
    repo = _StatefulRepo()
    repo._conv = _seed_cerrado()
    # Haiku terco: ghost-close
    anthropic = _fake_anthropic(["Listo, ya quedó agendada tu cita."])
    handler = AsyncMock(
        return_value=AppointmentHandlerResult(
            hint_para_prompt="[FLUJO AGENDADO — pide datos]", appointment_id=None
        )
    )

    from app.core.orchestrator import procesar_turno

    ctx = _patches(
        repo,
        anthropic,
        classify=AsyncMock(return_value=_intent(Intent.QUIERE_AGENDAR)),
        extract=AsyncMock(return_value=ExtraccionTurno(quiere_agendar=True)),
        handler=handler,
    )
    _enter(ctx)
    try:
        result = await procesar_turno(mensaje="quiero agendar otra cita", session_id="web:reuse")
    finally:
        _exit(ctx)

    # el re-armado reseteó cita_agendada → el validador vuelve a estar ARMADO
    assert "no_confirma_cita_inexistente" in result.validators_failed


@pytest.mark.asyncio
async def test_rearmado_captura_todo_del_mensaje_disparador() -> None:
    """FIX 2026-06-02b: el mensaje disparador del 2º agendado trae nombre+edad+
    día+hora → se capturan TODOS sobre el estado re-armado (no se borran), se
    deduce el grado y cierra en UN turno, sin re-preguntar nombre/edad/grado."""
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import FaseAgendado
    from app.tools.campus import CampusResult

    # "mañana" (no un día de semana) → la fecha la gobierna el extractor LLM
    # mockeado; este test verifica el RE-ARMADO + captura del disparador, no la
    # resolución determinística de día (cubierta en su propio test).
    msg = "quiero agendar otra para mi hija Lucía, 5 años, mañana 11am"

    async def fake_classify(message, **kw):
        return _intent(Intent.QUIERE_AGENDAR)

    async def fake_extract(mensaje, estado_actual, **kw):
        # el state-extractor captura nombre + edad del mismo mensaje
        return ExtraccionTurno(nombre_hijo="Lucía", edad_hijo=5, quiere_agendar=True)

    async def fake_extract_dt(mensaje, *, now=None):
        # 2026-06-11 es jueves
        return AppointmentDateTime(
            fecha="2026-06-11", hora="11:00", confidence=0.95, razonamiento="t"
        )

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )
    repo = _StatefulRepo()
    repo._conv = _seed_cerrado()
    anthropic = _fake_anthropic(["ok"])
    create_appt = AsyncMock(return_value=1000)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        result = await procesar_turno(mensaje=msg, session_id="web:reuse", canal=None, now=_NOW_MIE)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # TODO se capturó del mensaje disparador (no se borró al re-armar)
    assert capt.hijos[0].nombre == "Lucía"
    assert capt.hijos[0].edad == 5
    assert capt.hijos[0].grado == "3° de Kinder"  # deducido de 5 años
    # "mañana" (now=mié 3-jun) → jue 4-jun (extraer_fecha_relativa, no el mock).
    assert capt.cita_fecha_slot == "2026-06-04" and capt.cita_hora_slot == "11:00"
    # cerró en UN turno (todo venía + el papá ya estaba en el estado)
    create_appt.assert_awaited_once()
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "ya quedó agendada" in result.response
    assert "https://www.google.com/maps" in result.response


# ============================================================
# 10. REGRESIÓN PERMANENTE (2026-06-02): N agendados en UNA sesión →
#     N filas reales + grado correcto por edad. Incluye un turno forzado a
#     CONFUSO_OTRO (el clasificador LLM falla "quiero agendar otra") para
#     verificar que el trigger DETERMINÍSTICO re-arma igual. Y un grado parcial
#     ("kinder") que la deducción por edad debe sobreescribir.
# ============================================================


@pytest.mark.asyncio
async def test_tres_agendados_una_sesion_n_filas_y_grado_por_edad() -> None:
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state import (
        Canal,
        EstadoCapturado,
        EstadoConversacion,
        FaseAgendado,
        HijoInfo,  # noqa: F401  (importado por claridad del escenario)
    )
    from app.tools.campus import CampusResult

    # (intent del LLM, extracción, (fecha,hora)). Los agendados 2 y 3 los fuerzo a
    # CONFUSO_OTRO: el trigger determinístico ("quiero agendar otra") debe re-armar.
    SCRIPT = {
        "quiero agendar para Mateo, 4 años, el lunes 10am": (
            Intent.QUIERE_AGENDAR,
            ExtraccionTurno(nombre_hijo="Mateo", edad_hijo=4),
            ("2026-06-08", "10:00"),
        ),
        # ↓ CONFUSO_OTRO forzado + grado PARCIAL "kinder" (debe deducir 5→3° Kinder)
        "ahora quiero agendar otra para Lucía, 5 años, el martes 11am": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(
                nombre_hijo="Lucía", edad_hijo=5, nivel_buscado="kinder", grado_hijo="kinder"
            ),
            ("2026-06-09", "11:00"),
        ),
        "quiero agendar otra para Anabela, 6 años, el miércoles 9am": (
            Intent.CONFUSO_OTRO,
            ExtraccionTurno(nombre_hijo="Anabela", edad_hijo=6),
            ("2026-06-10", "09:00"),
        ),
    }

    async def fake_classify(message, **kw):
        return _intent(SCRIPT[message][0])

    async def fake_extract(mensaje, estado_actual, **kw):
        return SCRIPT[mensaje][1]

    async def fake_extract_dt(mensaje, *, now=None):
        f, h = SCRIPT[mensaje][2]
        return AppointmentDateTime(fecha=f, hora=h, confidence=0.95, razonamiento="t")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3", "primaria_1"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )

    repo = _StatefulRepo()
    repo._conv = EstadoConversacion(
        session_id="web:multi",
        canal=Canal.WEB,
        identificador="multi",
        estado_capturado=EstadoCapturado(
            nombre_papa="Oscar Rodriguez",
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
            fase_agendado=FaseAgendado.EXPLORANDO,
        ),
    )
    anthropic = _fake_anthropic(["ok"])
    create_appt = AsyncMock(side_effect=lambda **kw: 900 + create_appt.await_count)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    grados_por_turno = []
    _enter(leaf)
    try:
        for mensaje in SCRIPT:
            await procesar_turno(mensaje=mensaje, session_id="web:multi", canal=None)
            h = repo._conv.estado_capturado.hijos
            grados_por_turno.append((h[0].nombre, h[0].grado) if h else (None, None))
    finally:
        _exit(leaf)

    # N agendados → N filas reales (no ghost-close en ninguno)
    assert create_appt.await_count == 3
    # grado DEDUCIDO por edad en cada uno (no el de Haiku ni el parcial "kinder")
    assert grados_por_turno == [
        ("Mateo", "2° de Kinder"),  # 4 años
        ("Lucía", "3° de Kinder"),  # 5 años — sobreescribió el parcial "kinder"
        ("Anabela", "1° de Primaria"),  # 6 años — NO se salta Kinder 3°/Primaria 1°
    ]


# ============================================================
# 11. REGRESIÓN PERMANENTE (2026-06-02): PRIMER agendado con TODO en el primer
#     mensaje y un día CERCANO ("el viernes") → la fecha se resuelve
#     DETERMINÍSTICAMENTE al próximo viernes (sin preguntar "¿el 5 o el 12?"),
#     NO re-pregunta nada, y CIERRA con D.4 en UN turno. Prueba clave: el LLM
#     extract_datetime se mockea a VACÍO → si la cita igual se crea, es porque
#     el resolver determinístico cargó la fecha (no es load-bearing el LLM).
# ============================================================


@pytest.mark.asyncio
async def test_primer_agendado_dia_cercano_resuelve_determinista_y_cierra() -> None:
    import types
    from datetime import datetime, timedelta

    from app.core.appointment_extractor import TZ_MONTERREY, AppointmentDateTime
    from app.core.state import (
        Canal,
        EstadoCapturado,
        EstadoConversacion,
        FaseAgendado,
    )
    from app.tools.campus import CampusResult

    MENSAJE = (
        "hola quiero agendar mi hijo Mateo tiene 4 años yo soy Pedro Rojas, "
        "ing2oscar@gmail.com, +17866035862 el viernes 10am"
    )

    async def fake_classify(message, **kw):
        return _intent(Intent.QUIERE_AGENDAR)

    async def fake_extract(mensaje, estado_actual, **kw):
        return ExtraccionTurno(
            nombre_hijo="Mateo",
            edad_hijo=4,
            nombre_papa="Pedro Rojas",
            nombre_papa_explicito=True,
            email_papa="ing2oscar@gmail.com",
            telefono="+17866035862",
        )

    # El LLM de fecha FALLA (vacío, baja confianza). El resolver determinístico
    # ("el viernes" → próximo viernes) y extraer_hora_simple ("10am" → 10:00)
    # deben cargar los slots igual.
    async def fake_extract_dt(mensaje, *, now=None):
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="vacío")

    campus1 = CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )

    repo = _StatefulRepo()
    repo._conv = EstadoConversacion(
        session_id="web:pedro",
        canal=Canal.WEB,
        identificador="pedro",
        estado_capturado=EstadoCapturado(fase_agendado=FaseAgendado.EXPLORANDO),
    )
    anthropic = _fake_anthropic(["Voy a pasar tu solicitud a Lily."])  # Haiku improvisa…
    create_appt = AsyncMock(side_effect=lambda **kw: 900 + create_appt.await_count)

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=campus1)),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    from app.core.orchestrator import procesar_turno

    _enter(leaf)
    try:
        result = await procesar_turno(mensaje=MENSAJE, session_id="web:pedro", canal=None)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # 1) La cita se CREÓ en este único turno (NO ghost-close, NO "paso a Lily").
    assert create_appt.await_count == 1
    # 2) La fecha se resolvió DETERMINÍSTICAMENTE a un VIERNES futuro (weekday()==4).
    fecha_dt = datetime.strptime(capt.cita_fecha_slot, "%Y-%m-%d").replace(tzinfo=TZ_MONTERREY)
    assert fecha_dt.weekday() == 4, f"esperaba viernes, fue {capt.cita_fecha_slot}"
    assert fecha_dt.date() >= (datetime.now(TZ_MONTERREY) - timedelta(days=1)).date()
    assert capt.cita_hora_slot == "10:00"  # de extraer_hora_simple, no del LLM
    # 3) NO re-preguntó: el grado se DEDUJO (no pidió nombre/edad), nivel kinder.
    assert capt.hijos and capt.hijos[0].nombre == "Mateo"
    assert capt.hijos[0].grado == "2° de Kinder"
    # 4) Cerró con D.4 (override): la respuesta es la plantilla, NO el "paso a Lily".
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response
    assert "Lily" not in result.response


# ============================================================
# 12. REGRESIÓN INTEGRAL PERMANENTE (2026-06-02): toda la capa de captura es
#     DETERMINÍSTICA. El LLM aporta CERO (extraer_de_mensaje = solo reglas;
#     classify = CONFUSO_OTRO siempre; extract_datetime = vacío). Cubre los 5
#     escenarios que pidió el usuario en una sola suite permanente.
# ============================================================


def _campus_test():
    from app.tools.campus import CampusResult

    return CampusResult(
        id=1,
        nombre="Campus 1",
        direccion="José Figueroa Siller 156",
        colonia="Doctores",
        ciudad="Saltillo",
        estado="Coahuila",
        niveles=["kinder_1", "kinder_2", "kinder_3", "primaria_1"],
        google_maps_url="https://www.google.com/maps/search/?api=1&query=Jose",
    )


def _leaf_determinista(repo, anthropic, create_appt):
    """Patches donde el LLM NO aporta nada: el extractor de estado corre SOLO la
    capa determinística (ExtraccionTurno vacío + reglas), el intent es siempre
    CONFUSO_OTRO y el extractor de fecha LLM devuelve vacío. Así, si la cita se
    crea, es 100% por la capa determinística."""
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state_extractor import (
        ExtraccionTurno,
        _aplicar_fallbacks_deterministicos,
    )

    async def fake_classify(message, **kw):
        return _intent(Intent.CONFUSO_OTRO)  # el LLM de intent SIEMPRE falla

    async def fake_extract(mensaje, estado_actual, *, ultimo_assistant=None, **kw):
        # LLM vacío → SOLO la capa determinística (regex) captura. Incluye el
        # contexto del último turno de Sofía y el campo que pidió el gate.
        return _aplicar_fallbacks_deterministicos(
            ExtraccionTurno(),
            mensaje,
            ultimo_assistant=ultimo_assistant,
            ultimo_campo_pedido=estado_actual.ultimo_campo_pedido,
        )

    async def fake_extract_dt(mensaje, *, now=None):
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="vacío")

    return [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.resumen_disponibilidad",
            AsyncMock(return_value="lunes a viernes de 8:00 a.m. a 3:00 p.m."),
        ),
        patch(
            "app.core.appointment_flow.evaluar_dia",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True,
                    reason="ok",
                    alternativas=[],
                    resumen="lunes a viernes de 8:00 a.m. a 3:00 p.m.",
                )
            ),
        ),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=_campus_test())),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.update_lead", AsyncMock()),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]


def _nuevo_repo_explorando(session_id: str):
    from app.core.state import Canal, EstadoCapturado, EstadoConversacion, FaseAgendado

    repo = _StatefulRepo()
    repo._conv = EstadoConversacion(
        session_id=session_id,
        canal=Canal.WEB,
        identificador=session_id.split(":")[-1],
        estado_capturado=EstadoCapturado(fase_agendado=FaseAgendado.EXPLORANDO),
    )
    return repo


@pytest.mark.asyncio
async def test_captura_determinista_integral_5_escenarios() -> None:
    from datetime import datetime

    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    # ---- Escenario 1: TODO en un mensaje corrido → 1 turno, grado deducido, D.4 ----
    repo = _nuevo_repo_explorando("web:s1")
    create_appt = AsyncMock(side_effect=lambda **kw: 900 + create_appt.await_count)
    leaf = _leaf_determinista(repo, _fake_anthropic(["x"]), create_appt)
    msg1 = (
        "hola quiero agendar para mi hijo Mateo de 4 años, yo soy Pedro Rojas, "
        "ing2oscar@gmail.com, +17866035862, el viernes 10am"
    )
    _enter(leaf)
    try:
        r1 = await procesar_turno(mensaje=msg1, session_id="web:s1", canal=None)
    finally:
        _exit(leaf)
    c1 = repo._conv.estado_capturado
    assert create_appt.await_count == 1  # cerró en UN turno
    assert c1.fase_agendado == FaseAgendado.CERRADO
    assert c1.hijos[0].nombre == "Mateo" and c1.hijos[0].grado == "2° de Kinder"
    assert c1.email_papa == "ing2oscar@gmail.com" and c1.telefono == "+17866035862"
    assert c1.nombre_papa == "Pedro Rojas"
    # Escenario 5: día cercano "el viernes" → próximo VIERNES (no "¿5 o 12?")
    assert datetime.strptime(c1.cita_fecha_slot, "%Y-%m-%d").weekday() == 4
    assert c1.cita_hora_slot == "10:00"
    assert "https://www.google.com/maps" in r1.response and "Lily" not in r1.response

    # ---- Escenario 2: datos FRAGMENTADOS en varios mensajes → mismo resultado ----
    repo = _nuevo_repo_explorando("web:s2")
    create_appt2 = AsyncMock(side_effect=lambda **kw: 800 + create_appt2.await_count)
    leaf = _leaf_determinista(repo, _fake_anthropic(["x"]), create_appt2)
    fragmentos = [
        "quiero agendar una visita",
        "es para mi hijo Diego de 5 años",
        "yo soy Ana López",
        "mi correo ana@correo.mx y mi cel 8441234567",
        "el viernes a las 11am",
    ]
    _enter(leaf)
    try:
        rf = None
        for m in fragmentos:
            rf = await procesar_turno(mensaje=m, session_id="web:s2", canal=None)
    finally:
        _exit(leaf)
    c2 = repo._conv.estado_capturado
    assert create_appt2.await_count == 1
    assert c2.fase_agendado == FaseAgendado.CERRADO
    assert c2.hijos[0].nombre == "Diego" and c2.hijos[0].grado == "3° de Kinder"
    assert c2.nombre_papa == "Ana López"
    assert c2.email_papa == "ana@correo.mx" and c2.telefono == "8441234567"
    assert "https://www.google.com/maps" in rf.response

    # ---- Escenario 3: "pequeño"/"tiene" como nombre → PREGUNTA, no inventa ----
    repo = _nuevo_repo_explorando("web:s3")
    create_appt3 = AsyncMock(side_effect=lambda **kw: 700 + create_appt3.await_count)
    leaf = _leaf_determinista(repo, _fake_anthropic(["¿Cómo se llama tu peque?"]), create_appt3)
    _enter(leaf)
    try:
        await procesar_turno(
            mensaje="quiero agendar el viernes 10am", session_id="web:s3", canal=None
        )
        # parte el nombre real: solo dice "mi pequeño tiene 4 años"
        await procesar_turno(
            mensaje="es para mi pequeño, tiene 4 años", session_id="web:s3", canal=None
        )
    finally:
        _exit(leaf)
    c3 = repo._conv.estado_capturado
    assert create_appt3.await_count == 0  # NO creó (falta el nombre real)
    # NO inventó 'Pequeño' como nombre; quedó sin nombre → el gate lo pedirá.
    nombre_capt = c3.hijos[0].nombre if c3.hijos else None
    assert nombre_capt is None
    assert c3.fase_agendado == FaseAgendado.AGENDANDO  # sigue agendando, no cerró
    assert (c3.hijos[0].edad if c3.hijos else None) == 4  # la edad sí se capturó

    # ---- Escenario 4: 2º y 3º agendado en la MISMA sesión → N filas + grado ok ----
    # (continúa la sesión del escenario 1, que ya cerró el 1º con Mateo)
    repo = _nuevo_repo_explorando("web:s4")
    create_appt4 = AsyncMock(side_effect=lambda **kw: 600 + create_appt4.await_count)
    leaf = _leaf_determinista(repo, _fake_anthropic(["x"]), create_appt4)
    agendados = [
        "quiero agendar para mi hijo Mateo de 4 años, yo soy Pedro Rojas, "
        "ing2oscar@gmail.com, +17866035862, el viernes 10am",
        "quiero agendar otra para Lucía de 5 años el lunes 11am",
        "quiero agendar otra para Anabela de 6 años el martes 9am",
    ]
    grados = []
    _enter(leaf)
    try:
        for m in agendados:
            await procesar_turno(mensaje=m, session_id="web:s4", canal=None)
            h = repo._conv.estado_capturado.hijos
            grados.append((h[0].nombre, h[0].grado) if h else (None, None))
    finally:
        _exit(leaf)
    assert create_appt4.await_count == 3  # 3 filas reales
    assert grados == [
        ("Mateo", "2° de Kinder"),  # 4 años
        ("Lucía", "3° de Kinder"),  # 5 años
        ("Anabela", "1° de Primaria"),  # 6 años
    ]


# ============================================================
# 13. REGRESIÓN: el CIERRE y la PREGUNTA los decide el CÓDIGO, no Haiku.
#     Aquí Haiku está ACTIVO y es TERCO: en cada turno intenta re-pedir datos ya
#     dados e inventar "¿presencial o videollamada?". El test prueba que:
#       (a) el código cierra (crea cita + D.4) pese a que Haiku quiere seguir el
#           bucle — la respuesta final es la plantilla, no el texto de Haiku.
#       (b) durante la colección, el hint pide UN solo campo (el que falta) y
#           NUNCA re-pide un campo ya capturado.
#     Cubre el bug LIVE del multi-turno (el test 12 stubbeaba el LLM a cero y no
#     ejercía la generación de respuesta).
# ============================================================


class _HaikuTerco:
    """Anthropic falso que SIEMPRE intenta repreguntar e inventar modalidades
    (simula el bucle real). Graba el último mensaje de usuario (con el hint del
    flujo) de cada llamada para inspeccionar qué pidió el CÓDIGO."""

    _RESPUESTA = (
        "Para agendar necesito tu nombre completo y tu correo otra vez, por favor. "
        "¿Prefieres que la visita sea presencial o por videollamada?"
    )

    def __init__(self) -> None:
        self.hints: list[str] = []

    async def chat(self, *, system_blocks, messages, **kw):
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.hints.append(last_user)
        return _FakeMessage(self._RESPUESTA)


def _campo_pedido(hint: str) -> str | None:
    import re

    m = re.search(r"pregunta ÚNICAMENTE por: (.+?)\.", hint)
    return m.group(1).strip() if m else None


@pytest.mark.asyncio
async def test_codigo_decide_pregunta_y_cierre_con_haiku_terco() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:terco")
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 500 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    # Día+hora PRIMERO; luego los 6 datos llegan de a uno por turno.
    turnos = [
        "quiero agendar el viernes a las 10am",  # fija día+hora → AGENDANDO
        "se llama Emanuel",  # nombre del niño
        "tiene 4 años",  # edad → deduce grado
        "yo soy Oscar",  # nombre del papá
        "oscar@oscar.com",  # correo
        "8441234567",  # celular → completa → CIERRA
    ]

    _enter(leaf)
    try:
        respuestas = []
        result = None
        for t in turnos:
            result = await procesar_turno(mensaje=t, session_id="web:terco", canal=None)
            respuestas.append(result.response)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # (a) El CÓDIGO cerró pese al Haiku terco: 1 cita real + D.4 (no el texto de Haiku).
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response
    assert "videollamada" not in result.response  # el invento de Haiku NO sobrevive
    # Todos los datos quedaron capturados (sin perderse entre turnos).
    assert capt.hijos[0].nombre == "Emanuel" and capt.hijos[0].grado == "2° de Kinder"
    assert capt.nombre_papa == "Oscar"
    assert capt.email_papa == "oscar@oscar.com" and capt.telefono == "8441234567"

    # (b) Las respuestas de colección las generó el CÓDIGO (Haiku NO se llamó),
    # un solo campo por turno, en orden. (T1 fija día+hora → pide nombre del hijo.)
    assert len(haiku.hints) <= 1  # solo el turno de CIERRE llama a Haiku (D.4 lo sobreescribe)
    assert "nombre completo de tu hijo" in respuestas[0]
    assert "edad tiene Emanuel" in respuestas[1]
    assert "tu nombre completo" in respuestas[2]
    assert "correo electrónico" in respuestas[3]
    assert "número de celular" in respuestas[4]
    assert "https://www.google.com/maps" in respuestas[5]  # cierre D.4


# ============================================================
# 14. REGRESIÓN: el CIERRE es determinístico (bug real de Emanuel, 2026-06-02).
#     (a) El nombre del papá SUELTO ("Oscar Rodriguez") tras "¿tu nombre?" se
#         captura por contexto → el gate se completa y la cita se crea + D.4.
#     (b) Con los 6 datos completos + confirmación que el LLM marca confuso_otro
#         ("si ya te lo dije"), el CÓDIGO cierra: crea cita + D.4, NUNCA
#         "Lily te va a contactar".
# ============================================================


@pytest.mark.asyncio
async def test_nombre_papa_suelto_cierra_multiturno() -> None:
    """Multi-turno realista: el papá responde su nombre SUELTO ('Oscar Rodriguez')
    a la pregunta de Sofía. Antes quedaba nombre_papa=None y entraba en bucle;
    ahora se captura por contexto y la cita cierra."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:suelto")
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 400 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    turnos = [
        "quiero agendar el viernes a las 10am",  # día+hora
        "se llama Emanuel",  # nombre del niño
        "tiene 4 años",  # edad → grado
        "Oscar Rodriguez",  # ← nombre del papá SUELTO
        "oscar@oscar.com, 7866035862",  # correo + cel → completa → CIERRA
    ]
    _enter(leaf)
    try:
        result = None
        for t in turnos:
            result = await procesar_turno(mensaje=t, session_id="web:suelto", canal=None)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    assert capt.nombre_papa == "Oscar Rodriguez"  # ← se capturó el nombre suelto
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response
    assert "Lily te" not in result.response


@pytest.mark.asyncio
async def test_confirmacion_confuso_otro_con_datos_completos_cierra() -> None:
    """El turno de confirmación sale intent=confuso_otro. Con los 6 datos + slots
    ya completos, el CÓDIGO crea la cita + D.4 sin depender del intent. 'Lily te
    va a contactar' NO es alcanzable con los datos completos."""
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.orchestrator import procesar_turno
    from app.core.state import (
        Canal,
        EstadoCapturado,
        EstadoConversacion,
        FaseAgendado,
        HijoInfo,
        NivelEducativo,
    )
    from app.core.state_extractor import ExtraccionTurno

    # Estado con los 6 datos + slots YA completos, fase AGENDANDO (aún no cerró).
    repo = _StatefulRepo()
    repo._conv = EstadoConversacion(
        session_id="web:confirma",
        canal=Canal.WEB,
        identificador="confirma",
        estado_capturado=EstadoCapturado(
            nombre_papa="Oscar Rodriguez",
            email_papa="oscar@oscar.com",
            telefono="7866035862",
            nivel_buscado_actual=NivelEducativo.KINDER,
            hijos=[
                HijoInfo(
                    nombre="Emanuel", edad=4, nivel=NivelEducativo.KINDER, grado="2° de Kinder"
                )
            ],
            fase_agendado=FaseAgendado.AGENDANDO,
            cita_fecha_slot="2026-06-05",
            cita_hora_slot="15:00",
        ),
    )
    # Sofía venía confirmando los datos en su último turno.
    await repo.insert_message(
        "web:confirma", "assistant", "Perfecto, Oscar. Entonces te tengo todo, ¿confirmas?"
    )

    haiku = _HaikuTerco()  # si Haiku improvisara, NO debe sobrevivir
    create_appt = AsyncMock(return_value=321)

    async def fake_classify(message, **kw):
        return _intent(Intent.CONFUSO_OTRO)  # la confirmación NO se clasifica como agendar

    async def fake_extract(mensaje, estado_actual, **kw):
        return ExtraccionTurno()  # el papá no agrega datos nuevos, solo confirma

    async def fake_extract_dt(mensaje, *, now=None):
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="vacío")

    leaf = [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=haiku),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.resumen_disponibilidad",
            AsyncMock(return_value="lunes a viernes de 8:00 a.m. a 3:00 p.m."),
        ),
        patch(
            "app.core.appointment_flow.evaluar_dia",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True,
                    reason="ok",
                    alternativas=[],
                    resumen="lunes a viernes de 8:00 a.m. a 3:00 p.m.",
                )
            ),
        ),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=_campus_test())),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.update_lead", AsyncMock()),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]

    _enter(leaf)
    try:
        result = await procesar_turno(
            mensaje="si ya te lo dije", session_id="web:confirma", canal=None
        )
    finally:
        _exit(leaf)

    # El CÓDIGO cerró pese al intent confuso_otro y al Haiku terco.
    create_appt.assert_awaited_once()
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response
    assert "Lily te" not in result.response  # NO el "Lily te va a contactar" improvisado
    assert "videollamada" not in result.response


# ============================================================
# 15. REGRESIÓN: el apellido del HIJO NO se vuelve nombre del papá (bug Emanuel
#     Rodriguez, 2026-06-02). El papá da SOLO el nombre completo del hijo ("se
#     llama Emanuel Rodriguez") y nunca el suyo hasta que se lo preguntan →
#     Sofía DEBE preguntar "¿y tu nombre?"; nombre_papa != "Emanuel"/"Rodriguez".
# ============================================================


@pytest.mark.asyncio
async def test_apellido_hijo_no_se_vuelve_nombre_papa() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:apellido")
    haiku = _HaikuTerco()  # Haiku terco: si pudiera, re-preguntaría/inventaría
    create_appt = AsyncMock(side_effect=lambda **kw: 300 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    # El papá da el nombre COMPLETO del hijo, su edad, día, hora, correo y cel —
    # pero NUNCA su propio nombre, hasta el último turno cuando se lo preguntan.
    turnos_sin_nombre_papa = [
        "quiero agendar el viernes a las 10am",
        "se llama Emanuel Rodriguez",  # nombre+apellido del HIJO
        "tiene 4 años",
        "ema@ema.com, 7866035862",  # correo + cel (NO su nombre)
    ]
    _enter(leaf)
    try:
        respuestas = []
        for t in turnos_sin_nombre_papa:
            r = await procesar_turno(mensaje=t, session_id="web:apellido", canal=None)
            respuestas.append(r.response)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # El hijo conserva su nombre COMPLETO; el papá sigue SIN nombre → NO cerró.
    assert capt.hijos[0].nombre == "Emanuel Rodriguez"
    assert capt.nombre_papa is None  # ← jamás "Emanuel" ni "Rodriguez"
    assert create_appt.await_count == 0
    assert capt.fase_agendado == FaseAgendado.AGENDANDO  # sigue esperando el nombre

    # La última respuesta (generada por el CÓDIGO) pidió EXACTAMENTE el nombre del papá.
    assert haiku.hints == []
    assert "tu nombre completo" in respuestas[-1]

    # Ahora el papá SÍ da su nombre ("yo soy Oscar") → se captura y cierra.
    haiku2 = _HaikuTerco()
    leaf2 = _leaf_determinista(repo, haiku2, create_appt)
    _enter(leaf2)
    try:
        result = await procesar_turno(mensaje="yo soy Oscar", session_id="web:apellido", canal=None)
    finally:
        _exit(leaf2)
    capt = repo._conv.estado_capturado
    assert capt.nombre_papa == "Oscar"
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response


# ============================================================
# 16. REGRESIÓN: nombre del HIJO SUELTO tras "¿nombre de tu hijo?" (bug 2026-06-02,
#     sesión web:b764c105). Reproduce la conversación EXACTA: el papá responde
#     "Emanuel Rodriguez" suelto, "Oscar Rodriguez, 7866035862" juntos. Antes
#     nombre_hijo quedaba None → ghost-close ("Lily te contacta"). Ahora se captura
#     por contexto y cierra con D.4 cuando los 6 slots están llenos.
# ============================================================


@pytest.mark.asyncio
async def test_nombre_hijo_suelto_tras_pregunta_cierra() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:hijosuelto")
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 200 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    # Conversación EXACTA de la prueba real (web:b764c105):
    turnos = [
        "hola quiero agendar, mi pequeño tiene 4 años",  # edad → grado; nombre niño NO
        "jueves 11am",  # día+hora
        "Emanuel Rodriguez",  # ← nombre HIJO SUELTO (a "¿nombre de tu hijo?")
        "Oscar Rodriguez, 7866035862",  # ← nombre PAPÁ + tel juntos
        "ema@ema.com",  # correo → 6 slots llenos → CIERRA
    ]
    _enter(leaf)
    try:
        result = None
        for t in turnos:
            result = await procesar_turno(
                mensaje=t, session_id="web:hijosuelto", canal=None, now=_NOW_MIE
            )
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # Los 6 slots quedaron llenos por su propia vía.
    assert capt.hijos[0].nombre == "Emanuel Rodriguez"  # ← capturado por contexto
    assert capt.hijos[0].grado == "2° de Kinder"
    assert capt.nombre_papa == "Oscar Rodriguez"  # papá + tel juntos
    assert capt.email_papa == "ema@ema.com" and capt.telefono == "7866035862"
    assert capt.cita_fecha_slot == "2026-06-04" and capt.cita_hora_slot == "11:00"
    # Cerró con D.4, NO "Lily te va a contactar".
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response
    assert "Lily te" not in result.response


@pytest.mark.asyncio
async def test_pide_un_solo_campo_a_la_vez_sin_bundlear() -> None:
    """El CÓDIGO pide UN campo a la vez con plantilla fija: la RESPUESTA es del
    código (Haiku no se llama en colección), nunca bundlea ni improvisa."""
    from app.core.orchestrator import procesar_turno

    repo = _nuevo_repo_explorando("web:unocampo")
    haiku = _HaikuTerco()  # si se llamara, metería "videollamada" — NO debe pasar
    create_appt = AsyncMock(side_effect=lambda **kw: 100 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    turnos = [
        "quiero agendar el viernes a las 10am",
        "se llama Diego Pérez",  # nombre hijo
        "tiene 5 años",  # edad
        "yo soy Marta",  # nombre papá
        "marta@x.com",  # correo
        "8441234567",  # cel → cierra
    ]
    _enter(leaf)
    try:
        respuestas = []
        for t in turnos:
            r = await procesar_turno(mensaje=t, session_id="web:unocampo", canal=None)
            respuestas.append(r.response)
    finally:
        _exit(leaf)

    # Las 5 primeras son preguntas determinísticas de UN solo campo, en orden.
    assert "nombre completo de tu hijo" in respuestas[0]
    assert "edad tiene Diego Pérez" in respuestas[1]
    assert "tu nombre completo" in respuestas[2]
    assert "correo electrónico" in respuestas[3]
    assert "número de celular" in respuestas[4]
    # Haiku NO se llamó en colección → su invento no aparece nunca.
    for r in respuestas[:5]:
        assert "videollamada" not in r
    assert len(haiku.hints) <= 1  # solo el turno de CIERRE llama a Haiku (D.4 lo sobreescribe)
    # La 6ª cierra con D.4 (override de cierre).
    assert "https://www.google.com/maps" in respuestas[5]


# ============================================================
# 17. REGRESIÓN INTEGRAL (2026-06-04): el caso de MARÍA de punta a punta.
#     Bundle "X, hijo Y" (papá + hijo en un turno) + grado declarado "primero de
#     primaria" sin edad. Antes: ghost-close (nombre_papa=None), edad pedida al
#     final, grado pisado a 2°, Maps improvisado. Ahora: CIERRA con D.4,
#     nombre_papa="Maria Urdaneta", grado="1° de Primaria" (Política A), Campus 1.
# ============================================================


@pytest.mark.asyncio
async def test_maria_bundle_y_grado_declarado_cierra_e2e() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado, HijoInfo, NivelEducativo

    repo = _nuevo_repo_explorando("web:maria")
    # Entra a la colección ya con el grado DECLARADO (como en T1 "primero de
    # primaria", ya canonicalizado) y SIN edad — el patrón que rompía.
    repo._conv.estado_capturado.hijos = [
        HijoInfo(nombre=None, edad=None, nivel=NivelEducativo.PRIMARIA, grado="1° de Primaria")
    ]
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 600 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    turnos = [
        "quiero agendar el viernes a las 10am",  # día+hora → AGENDANDO
        "maria urdaneta, hijo juan david wilchez",  # BUNDLE: papá + hijo en un turno
        "tiene 7 años",  # edad (pedida EN ORDEN, no al final)
        "ingenieriademarketing@gmail.com, 6622236125",  # correo + cel → 6 datos → CIERRA
    ]
    _enter(leaf)
    try:
        result = None
        for t in turnos:
            result = await procesar_turno(mensaje=t, session_id="web:maria", canal=None)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # Bundle capturó AMBOS nombres en su slot correcto.
    assert capt.nombre_papa == "Maria Urdaneta"
    assert capt.hijos[0].nombre == "Juan David Wilchez"
    # Política A: la edad (7) está, PERO el grado DECLARADO ("1° de Primaria") manda
    # — NO se pisó con el derivado por edad (que sería 2°).
    assert capt.hijos[0].edad == 7
    assert capt.hijos[0].grado == "1° de Primaria"
    assert capt.email_papa == "ingenieriademarketing@gmail.com"
    assert capt.telefono == "6622236125"
    # CERRÓ con D.4 + Maps de TABLA (Campus 1), no "Lily te contacta" ni "solicitud".
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert capt.campus_cita == "Campus 1"
    assert "https://www.google.com/maps" in result.response
    assert "Lily te" not in result.response and "solicitud" not in result.response


# ============================================================
# 18. REGRESIÓN (2026-06-04): "HOY" se resuelve y la pregunta del día NO se repite.
#     Antes: "hoy" no se parseaba → la pregunta del día se re-renderizaba idéntica.
# ============================================================


@pytest.mark.asyncio
async def test_hoy_se_resuelve_y_no_repite_pregunta_del_dia() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _nuevo_repo_explorando("web:hoy")
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 700 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    _enter(leaf)
    try:
        # T1: sin fecha → el código pide el DÍA.
        r1 = await procesar_turno(
            mensaje="quiero agendar una visita", session_id="web:hoy", canal=None, now=_NOW_MIE
        )
        # T2: "hoy" (mié 3-jun 9am) → resuelve a HOY (3-jun), NO repite la pregunta del día.
        r2 = await procesar_turno(mensaje="hoy", session_id="web:hoy", canal=None, now=_NOW_MIE)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    assert capt.cita_fecha_slot == "2026-06-03"  # "hoy" resuelto, no None
    assert "qué día" in r1.response.lower()  # T1 pidió el día
    assert r2.response != r1.response  # T2 NO repite idéntico
    assert "qué hora" in r2.response.lower()  # ya avanzó: ahora pide la hora


# ============================================================
# 19. REGRESIÓN (2026-06-04): la HORA suelta "10" se parsea y cierra; y NINGÚN
#     campo re-renderiza idéntica la pregunta cuando la respuesta no parsea.
# ============================================================


@pytest.mark.asyncio
async def test_hora_suelta_10_se_parsea_y_cierra() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import (
        EstadoCapturado,
        FaseAgendado,
        HijoInfo,
        NivelEducativo,
    )

    repo = _nuevo_repo_explorando("web:hora")
    # En AGENDANDO con los 6 datos y el DÍA listos; falta solo la hora (el código
    # acaba de pedirla → ultimo_campo_pedido='hora').
    repo._conv.estado_capturado = EstadoCapturado(
        nombre_papa="Oscar Rodriguez",
        email_papa="o@x.com",
        telefono="7866035862",
        nivel_buscado_actual=NivelEducativo.KINDER,
        hijos=[HijoInfo(nombre="Mateo", edad=4, nivel=NivelEducativo.KINDER, grado="2° de Kinder")],
        fase_agendado=FaseAgendado.AGENDANDO,
        cita_fecha_slot="2026-06-11",
        cita_hora_slot=None,
        ultimo_campo_pedido="hora",
    )
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 800 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)
    _enter(leaf)
    try:
        result = await procesar_turno(mensaje="10", session_id="web:hora", canal=None, now=_NOW_MIE)
    finally:
        _exit(leaf)
    capt = repo._conv.estado_capturado
    assert capt.cita_hora_slot == "10:00"  # "10" → 10:00 (estaba en las opciones)
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert "https://www.google.com/maps" in result.response


@pytest.mark.asyncio
async def test_respuesta_no_parseable_no_repite_pregunta_identica() -> None:
    """Guard anti-bucle: si la respuesta de un campo no parsea, la re-pregunta NO
    es idéntica — se reformula con el formato esperado."""
    from app.core.orchestrator import procesar_turno

    repo = _nuevo_repo_explorando("web:bucle")
    haiku = _HaikuTerco()
    create_appt = AsyncMock(side_effect=lambda **kw: 900 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)
    _enter(leaf)
    try:
        # T1: da el día → el código pide la HORA (primera vez, base).
        r1 = await procesar_turno(
            mensaje="quiero agendar el jueves", session_id="web:bucle", canal=None, now=_NOW_MIE
        )
        # T2: respuesta que NO parsea como hora → re-pregunta, pero NO idéntica.
        r2 = await procesar_turno(
            mensaje="mmm no sé bien", session_id="web:bucle", canal=None, now=_NOW_MIE
        )
    finally:
        _exit(leaf)

    assert "qué hora" in r1.response.lower()  # T1 pidió la hora (base)
    assert r2.response != r1.response  # T2 NO repite idéntica
    assert "no te entendí" in r2.response.lower()  # reformuló con disculpa + formato
    assert "qué hora" in r2.response.lower()  # sigue pidiendo la hora


# ============================================================
# 20. REGRESIÓN INTEGRAL (2026-06-04): booking COMPLETO con la respuesta natural
#     MÁS SIMPLE en CADA campo. Ejercita el parseo de todos los campos en un solo
#     test para no volver a descubrir fallos campo por campo en vivo.
# ============================================================


@pytest.mark.asyncio
async def test_booking_completo_respuesta_natural_minima_por_campo() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:natural")
    haiku = _HaikuTerco()  # Haiku activo: solo lo llamaría en el cierre (D.4 lo override)
    create_appt = AsyncMock(side_effect=lambda **kw: 950 + create_appt.await_count)
    leaf = _leaf_determinista(repo, haiku, create_appt)

    # La respuesta MÁS SIMPLE que daría un papá real, campo por campo.
    turnos = [
        "quiero agendar para primero de primaria",  # nivel/grado declarado
        "mañana",  # día (relativa)
        "10",  # hora (número suelto)
        "emanuel rodriguez",  # nombre del hijo (suelto)
        "5",  # edad (número suelto)
        "oscar rodriguez",  # nombre del papá (suelto)
        "oscar@correo.com",  # correo
        "7866035862",  # teléfono → CIERRA
    ]
    respuestas = []
    _enter(leaf)
    try:
        for t in turnos:
            r = await procesar_turno(mensaje=t, session_id="web:natural", canal=None, now=_NOW_MIE)
            respuestas.append(r.response)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # Cada campo se capturó de su respuesta natural mínima.
    assert capt.cita_fecha_slot == "2026-06-04"  # "mañana"
    assert capt.cita_hora_slot == "10:00"  # "10"
    assert capt.hijos[0].nombre == "Emanuel Rodriguez"
    assert capt.hijos[0].edad == 5  # "5"
    assert capt.hijos[0].grado == "1° de Primaria"  # declarado, Política A (no 1° por edad 5)
    assert capt.nombre_papa == "Oscar Rodriguez"
    assert capt.email_papa == "oscar@correo.com"
    assert capt.telefono == "7866035862"
    # CIERRA con D.4 + Campus 1 (primaria 1°), sin "solicitud"/"Lily te".
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert capt.campus_cita == "Campus 1"
    assert "ya quedó agendada" in respuestas[-1]
    assert "https://www.google.com/maps" in respuestas[-1]
    assert "solicitud" not in respuestas[-1] and "Lily te" not in respuestas[-1]
    # Ningún turno de colección se quedó pegado repitiendo (todos distintos o avanzando).
    assert "no te entendí" not in " ".join(respuestas).lower()


# ============================================================
# 21. REGRESIÓN ESTRUCTURAL (2026-06-04): el CÓDIGO es dueño de TODOS los turnos de
#     colección, por el MISMO camino de producción donde Haiku PUEDE meterse
#     (clasificador realista + Haiku que improvisa bundle/fin de semana). Réplica
#     EXACTA de la secuencia que falló en vivo.
# ============================================================


class _HaikuMalo:
    """Haiku que SIEMPRE improvisa una pregunta de colección mala (junta campos y
    ofrece fin de semana). Si el código lo dejara armar la colección, esto se
    filtraría — el test prueba que NO."""

    _RESPUESTA = (
        "¿Me das el nombre y la edad de tu peque? ¿Te viene bien el sábado o el fin de semana?"
    )

    def __init__(self) -> None:
        self.llamadas = 0

    async def chat(self, *, system_blocks, messages, **kw):
        self.llamadas += 1
        return _FakeMessage(self._RESPUESTA)


def _leaf_produccion(repo, anthropic, create_appt, intents: dict):
    """Como _leaf_determinista pero con clasificador REALISTA (per-mensaje, incluye
    intents SUSTANTIVOS) — el camino donde Haiku puede meterse."""
    import types

    from app.core.appointment_extractor import AppointmentDateTime
    from app.core.state_extractor import (
        ExtraccionTurno,
        _aplicar_fallbacks_deterministicos,
    )

    async def fake_classify(message, **kw):
        return _intent(intents.get(message, Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO))

    async def fake_extract(mensaje, estado_actual, *, ultimo_assistant=None, **kw):
        return _aplicar_fallbacks_deterministicos(
            ExtraccionTurno(),
            mensaje,
            ultimo_assistant=ultimo_assistant,
            ultimo_campo_pedido=estado_actual.ultimo_campo_pedido,
        )

    async def fake_extract_dt(mensaje, *, now=None):
        return AppointmentDateTime(fecha=None, hora=None, confidence=0.0, razonamiento="vacío")

    return [
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=[])),
        patch("app.core.appointment_flow.extract_datetime", side_effect=fake_extract_dt),
        patch(
            "app.core.appointment_flow.resumen_disponibilidad",
            AsyncMock(return_value="lunes a viernes de 8:00 a.m. a 3:00 p.m."),
        ),
        patch(
            "app.core.appointment_flow.evaluar_dia",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True,
                    reason="ok",
                    alternativas=[],
                    resumen="lunes a viernes de 8:00 a.m. a 3:00 p.m.",
                )
            ),
        ),
        patch(
            "app.core.appointment_flow.is_slot_available",
            AsyncMock(
                return_value=types.SimpleNamespace(
                    available=True, reason="ok", alternativas=[], resumen=""
                )
            ),
        ),
        patch("app.core.appointment_flow.create_appointment", create_appt),
        patch("app.core.appointment_flow.get_campus_by_id", AsyncMock(return_value=_campus_test())),
        patch("app.core.appointment_flow.get_lead_by_session", AsyncMock(return_value=None)),
        patch("app.core.appointment_flow.create_lead", AsyncMock(return_value=88)),
        patch("app.core.appointment_flow.update_lead", AsyncMock()),
        patch("app.core.appointment_flow.emit_event", AsyncMock()),
        patch("app.core.appointment_flow.send_email", AsyncMock()),
        patch("app.core.appointment_flow.advance_stage_if_lower", AsyncMock(return_value=True)),
    ]


@pytest.mark.asyncio
async def test_camino_produccion_codigo_dueno_de_la_coleccion() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _nuevo_repo_explorando("web:prod")
    haiku = _HaikuMalo()
    create_appt = AsyncMock(side_effect=lambda **kw: 11 + create_appt.await_count)
    # Clasificador realista: "primero de primaria" y "mandan tareas?" SUSTANTIVOS.
    intents = {
        "hola": Intent.SALUDO_INICIAL,
        "primero de primaria": Intent.PREGUNTA_NIVEL,
        "mandan tareas?": Intent.OBJECION_TAREA,
        "quiero visitar el colegio": Intent.QUIERE_AGENDAR,
    }
    leaf = _leaf_produccion(repo, haiku, create_appt, intents)

    turnos = [
        "hola",
        "primero de primaria",  # sustantiva (Haiku) — pero captura grado por extracción
        "mandan tareas?",  # sustantiva (Haiku, postura tareas)
        "quiero visitar el colegio",  # → AGENDANDO; el código pide el día
        "hoy",
        "10",
        "emanuel rodriguez",
        "5",
        "oscar rodriguez",
        "oscar@correo.com",
        "7866035862",
    ]
    respuestas = []
    _enter(leaf)
    try:
        for t in turnos:
            r = await procesar_turno(mensaje=t, session_id="web:prod", canal=None, now=_NOW_MIE)
            respuestas.append(r.response)
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    # Slots: cada dato quedó en su lugar (fuente de verdad), edad incluida.
    assert capt.cita_fecha_slot == "2026-06-03"  # "hoy" (mié, antes del cierre)
    assert capt.cita_hora_slot == "10:00"  # "10" NO se perdió
    assert capt.hijos[0].nombre == "Emanuel Rodriguez"
    assert capt.hijos[0].edad == 5
    assert capt.hijos[0].grado == "1° de Primaria"
    assert capt.nombre_papa == "Oscar Rodriguez"
    # Las respuestas de COLECCIÓN (turnos del agendado) las generó el CÓDIGO:
    # nunca el bundle ni el "fin de semana" de Haiku.
    resp_coleccion = respuestas[3:]  # desde "quiero visitar" en adelante
    junto = " ".join(resp_coleccion).lower()
    assert "sábado" not in junto and "fin de semana" not in junto  # NUNCA fin de semana
    assert "nombre y la edad" not in junto  # NUNCA bundle de Haiku
    # La EDAD se pidió EXACTAMENTE una vez (no re-preguntada tras tener 5).
    n_edad = sum(1 for r in resp_coleccion if "qué edad" in r.lower())
    assert n_edad == 1, f"edad preguntada {n_edad} veces"
    # Cierre con D.4, Campus 1, 10:00.
    assert create_appt.await_count == 1
    assert capt.fase_agendado == FaseAgendado.CERRADO
    assert capt.campus_cita == "Campus 1"
    assert "ya quedó agendada" in respuestas[-1]
    assert "3:00 p.m." in respuestas[-1] or "10:00" in respuestas[-1] or "a.m." in respuestas[-1]
    assert "https://www.google.com/maps" in respuestas[-1]


@pytest.mark.asyncio
async def test_pregunta_costos_mid_coleccion_pausa_da_dato_y_reofrece() -> None:
    """Pregunta de COSTOS en mitad de la colección (paso hora): PAUSA → emite el dato
    correcto por código y RE-OFRECE la visita pidiendo la hora; el slot del día PERSISTE."""
    from app.core.orchestrator import procesar_turno

    repo = _nuevo_repo_explorando("web:mid")
    haiku = _HaikuMalo()
    create_appt = AsyncMock(side_effect=lambda **kw: 33 + create_appt.await_count)
    intents = {
        "quiero agendar": Intent.QUIERE_AGENDAR,
        "cuánto cuesta?": Intent.PREGUNTA_COSTOS,  # DATA, MID-colección
    }
    leaf = _leaf_produccion(repo, haiku, create_appt, intents)
    _enter(leaf)
    try:
        await procesar_turno(
            mensaje="quiero agendar", session_id="web:mid", canal=None, now=_NOW_MIE
        )
        await procesar_turno(mensaje="hoy", session_id="web:mid", canal=None, now=_NOW_MIE)  # día
        r = await procesar_turno(
            mensaje="cuánto cuesta?", session_id="web:mid", canal=None, now=_NOW_MIE
        )
    finally:
        _exit(leaf)

    capt = repo._conv.estado_capturado
    assert capt.cita_fecha_slot == "2026-06-03"  # el día PERSISTE (no se perdió)
    assert capt.cita_hora_slot is None  # NO avanzó: sigue faltando la hora
    assert "💰" in r.response  # dato de costos emitido por código
    assert "a qué hora" in r.response.lower()  # re-oferta pidiendo la hora de ese día
