"""Inyección determinística de costos/horarios/estancias (réplica de la
conversación de Lili). El dato lo pone el CÓDIGO desde las tablas; Haiku no inventa.

Se mockean los tools (no red) con los valores REALES de las tablas y se captura el
mensaje que recibe Haiku para verificar que el bloque DATO OFICIAL es correcto.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from app.core.intent_classifier import Intent, IntentResult
from app.core.state_extractor import ExtraccionTurno, _aplicar_fallbacks_deterministicos
from app.tools.estancias import EstanciaResult
from app.tools.horarios import HorarioResult
from app.tools.precios import PrecioResult


def _precio_kinder() -> PrecioResult:
    return PrecioResult(
        nivel="kinder", sub_nivel="preschool", ciclo_escolar="2026-2027",
        inscripcion=Decimal("10000"), colegiatura_mensual=Decimal("5250"),
        seguro_escolar=None, seguro_orfandad=None, recursos_educativos=None,
        gastos_escolares=None, desayunos_snacks=None, talleres=None,
        cuota_graduacion=None, total_gastos_iniciales=None, num_colegiaturas=11,
        fecha_limite_pago=None, notas=None,
    )


def _horario_kinder2() -> HorarioResult:
    return HorarioResult(
        nivel="kinder_2", modalidad="regular",
        hora_inicio="09:00:00", hora_fin="14:00:00", dias="L-V", notas=None,
    )


def _estancias_kinder() -> list[EstanciaResult]:
    return [
        EstanciaResult(nombre="media", aplica_para=["kinder"], hora_inicio="07:00:00",
                       hora_fin="15:30:00", incluye_comida=True, incluye_snack=False,
                       incluye_academia=False, costo_mensual=Decimal("1400"),
                       costo_por_dia=None, inscripcion_extra=None, notas=None),
        EstanciaResult(nombre="after_school", aplica_para=["kinder"], hora_inicio="07:00:00",
                       hora_fin="19:00:00", incluye_comida=True, incluye_snack=True,
                       incluye_academia=True, costo_mensual=Decimal("3100"),
                       costo_por_dia=None, inscripcion_extra=None, notas=None),
    ]


class _RecordingAnthropic:
    """Captura el último mensaje de usuario (con el bloque inyectado) y, como Haiku
    BIEN portado, devuelve textual el bloque DATO OFICIAL que recibió."""

    def __init__(self) -> None:
        self.user_msgs: list[str] = []

    async def chat(self, *, system_blocks, messages, **kw):
        import types

        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.user_msgs.append(last)
        texto = last.split("DATO OFICIAL")[-1] if "DATO OFICIAL" in last else "ok"
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=10,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=texto)], usage=usage)


class _Repo:
    def __init__(self, conv):
        self._conv = conv
        self._messages: list = []

    async def get_conversation(self, session_id):
        return self._conv

    async def upsert_conversation(self, estado):
        self._conv = estado

    async def list_recent_messages(self, session_id, limit=20):
        return self._messages[-limit:]

    async def insert_message(self, session_id, role, content, **kw):
        self._messages.append({"role": role, "content": content})

    async def insert_turn_log(self, **kw):
        pass

    async def count_turns(self, session_id):
        return sum(1 for m in self._messages if m["role"] == "assistant")


def _leaf(repo, anthropic, classify_intent_value):
    from app.config import get_settings as _gs

    async def fake_classify(message, **kw):
        return IntentResult(intent=classify_intent_value, confidence=0.9)

    async def fake_extract(mensaje, estado_actual, *, ultimo_assistant=None, **kw):
        return _aplicar_fallbacks_deterministicos(
            ExtraccionTurno(), mensaje, ultimo_assistant=ultimo_assistant,
            ultimo_campo_pedido=estado_actual.ultimo_campo_pedido,
        )

    settings_sin_validadores = _gs().model_copy(update={"enable_validators": False})
    return [
        patch("app.core.orchestrator.get_settings", return_value=settings_sin_validadores),
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=_precio_kinder())),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=_horario_kinder2())),
        patch("app.core.orchestrator.get_estancias", AsyncMock(return_value=_estancias_kinder())),
    ]


def _estado_kinder2():
    from app.core.state import Canal, EstadoCapturado, EstadoConversacion, HijoInfo, NivelEducativo
    return EstadoConversacion(
        session_id="web:lili", canal=Canal.WEB, identificador="lili",
        estado_capturado=EstadoCapturado(
            nivel_buscado_actual=NivelEducativo.KINDER,
            hijos=[HijoInfo(nivel=NivelEducativo.KINDER, grado="2° de Kinder")],
        ),
    )


def _enter(ctx):
    for c in ctx:
        c.__enter__()


def _exit(ctx):
    for c in reversed(ctx):
        c.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_costos_kinder_inyecta_5250_no_6500() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_estado_kinder2())
    haiku = _RecordingAnthropic()
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿costos de kinder?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    inyectado = haiku.user_msgs[0]
    assert "$5,250" in inyectado and "$10,000" in inyectado
    assert "$6,500" not in inyectado and "6,500" not in r.response


@pytest.mark.asyncio
async def test_horario_2k_inyecta_9_a_2_no_8_230() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_estado_kinder2())
    haiku = _RecordingAnthropic()
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿horario?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    inyectado = haiku.user_msgs[0]
    assert "9:00 a.m. a 2:00 p.m." in inyectado
    assert "8:00" not in inyectado and "2:30" not in inyectado
    assert "2:30" not in r.response


@pytest.mark.asyncio
async def test_estancias_inyecta_7_a_7_no_530() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_estado_kinder2())
    haiku = _RecordingAnthropic()
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_ESTANCIAS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿estancias?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    inyectado = haiku.user_msgs[0]
    assert "7:00 a.m. a 7:00 p.m." in inyectado
    assert "5:30" not in inyectado and "5:30" not in r.response


@pytest.mark.asyncio
async def test_costos_durante_agendado_tambien_inyecta() -> None:
    """Misma pregunta DURANTE el agendado → el bloque correcto se inyecta igual."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    conv = _estado_kinder2()
    conv.estado_capturado.fase_agendado = FaseAgendado.AGENDANDO  # en mitad del agendado
    repo = _Repo(conv)
    haiku = _RecordingAnthropic()
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    # En agendado corre el appointment_handler; lo neutralizamos (no es el foco).
    ctx.append(patch("app.core.orchestrator.handle_appointment_intent",
                     AsyncMock(return_value=None)))
    _enter(ctx)
    try:
        await procesar_turno(mensaje="oye y ¿cuánto cuesta?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    inyectado = haiku.user_msgs[0]
    assert "$5,250" in inyectado  # el dato oficial llega aunque sea mid-agendado


@pytest.mark.asyncio
async def test_kinder_sin_grado_pide_grado_no_inventa_horario() -> None:
    """Kinder SIN grado → el bloque dice que falta el grado (no se inventa horario)."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import Canal, EstadoCapturado, EstadoConversacion, HijoInfo, NivelEducativo

    conv = EstadoConversacion(
        session_id="web:sg", canal=Canal.WEB, identificador="sg",
        estado_capturado=EstadoCapturado(nivel_buscado_actual=NivelEducativo.KINDER,
                                          hijos=[HijoInfo(nivel=NivelEducativo.KINDER)]),
    )
    repo = _Repo(conv)
    haiku = _RecordingAnthropic()
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="¿horario de kinder?", session_id="web:sg", canal=None)
    finally:
        _exit(ctx)
    inyectado = haiku.user_msgs[0]
    assert "grado" in inyectado.lower()  # pide el grado
    assert "8:00" not in inyectado and "9:00 a.m. a 2:00 p.m." not in inyectado
