"""Costos/horarios/estancias: la cifra la EMITE el CÓDIGO y un GUARD borra cualquier
número que Haiku invente. Réplica del turno REAL de Lili (bundleado, intent=confuso_otro,
Haiku devolviendo números equivocados) → la respuesta final muestra los datos correctos.

NO se prueba "Haiku se portó bien": el fake Haiku devuelve $6,450 / 8:00 a propósito,
y se afirma que la respuesta IGUAL muestra $5,250 / $10,000 / 9:00-2:00.
"""

from __future__ import annotations

import types
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
    return HorarioResult(nivel="kinder_2", modalidad="regular",
                         hora_inicio="09:00:00", hora_fin="14:00:00", dias="L-V", notas=None)


def _estancias_kinder() -> list[EstanciaResult]:
    return [
        EstanciaResult(nombre="after_school", aplica_para=["kinder"], hora_inicio="07:00:00",
                       hora_fin="19:00:00", incluye_comida=True, incluye_snack=True,
                       incluye_academia=True, costo_mensual=Decimal("3100"),
                       costo_por_dia=None, inscripcion_extra=None, notas=None),
    ]


# Haiku que INVENTA números equivocados (lo que hizo en vivo): $6,450 / $2,150 / 8:00-1:00.
_HAIKU_MENTIROSO = (
    "¡Hola! Qué gusto. Tu peque va a 2° de Kinder.\n"
    "Colegiatura mensual: $6,450\n"
    "Inscripción anual: $2,150\n"
    "Horario escolar: 8:00 a.m. a 1:00 p.m.\n"
    "¿Qué buscas para él en esta etapa?"
)


class _Haiku:
    def __init__(self, texto: str = _HAIKU_MENTIROSO) -> None:
        self.texto = texto

    async def chat(self, *, system_blocks, messages, **kw):
        usage = types.SimpleNamespace(input_tokens=10, output_tokens=10,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self.texto)], usage=usage)


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


def _leaf(repo, anthropic, intent_value, *, estancias=None):
    from app.config import get_settings as _gs

    async def fake_classify(message, **kw):
        return IntentResult(intent=intent_value, confidence=0.9)

    async def fake_extract(mensaje, estado_actual, *, ultimo_assistant=None, **kw):
        return _aplicar_fallbacks_deterministicos(
            ExtraccionTurno(), mensaje, ultimo_assistant=ultimo_assistant,
            ultimo_campo_pedido=estado_actual.ultimo_campo_pedido,
        )

    s = _gs().model_copy(update={"enable_validators": False})
    return [
        patch("app.core.orchestrator.get_settings", return_value=s),
        patch("app.core.orchestrator.get_repository", return_value=repo),
        patch("app.core.orchestrator.get_anthropic", return_value=anthropic),
        patch("app.core.orchestrator.classify_intent", side_effect=fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", side_effect=fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.get_precio", AsyncMock(return_value=_precio_kinder())),
        patch("app.core.orchestrator.get_todos_precios", AsyncMock(return_value=[])),
        patch("app.core.orchestrator.get_horario", AsyncMock(return_value=_horario_kinder2())),
        patch("app.core.orchestrator.get_estancias",
              AsyncMock(return_value=_estancias_kinder() if estancias is None else estancias)),
    ]


def _conv(nivel, grado):
    from app.core.state import Canal, EstadoCapturado, EstadoConversacion, HijoInfo
    return EstadoConversacion(
        session_id="web:lili", canal=Canal.WEB, identificador="lili",
        estado_capturado=EstadoCapturado(
            nivel_buscado_actual=nivel,
            hijos=[HijoInfo(nivel=nivel, grado=grado)] if (nivel or grado) else [],
        ),
    )


def _enter(ctx):
    for c in ctx:
        c.__enter__()


def _exit(ctx):
    for c in reversed(ctx):
        c.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_turno_real_lili_bundleado_confuso_otro_codigo_gana() -> None:
    """RÉPLICA DEL TURNO REAL: mensaje bundleado, intent=confuso_otro, Haiku miente
    ($6,450/$2,150/8:00). La respuesta final IGUAL muestra los datos correctos."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import NivelEducativo

    # El estado se llena por la EXTRACCIÓN real del mensaje ("2do de kinder").
    repo = _Repo(_conv(None, None))
    haiku = _Haiku(_HAIKU_MENTIROSO)
    ctx = _leaf(repo, haiku, Intent.CONFUSO_OTRO)  # ← el intent REAL que falló
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="Hola, quiero informes para kinder, costos y horarios viene de otra "
                    "escuela, va a 2do de kinder",
            session_id="web:lili", canal=None,
        )
    finally:
        _exit(ctx)

    # El estado capturó kinder + 2° de Kinder.
    assert repo._conv.estado_capturado.nivel_buscado_actual == NivelEducativo.KINDER
    assert repo._conv.estado_capturado.hijos[0].grado == "2° de Kinder"
    # CÓDIGO emitió los datos correctos:
    assert "$5,250" in r.response and "$10,000" in r.response
    assert "9:00 a.m. a 2:00 p.m." in r.response
    # GUARD borró los inventos de Haiku:
    assert "$6,450" not in r.response and "$2,150" not in r.response
    assert "8:00 a.m. a 1:00 p.m." not in r.response and "1:00 p.m." not in r.response


@pytest.mark.asyncio
async def test_costos_emite_5250_y_guard_borra_6450() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("Colegiatura: $6,450 al mes. ¡Te encantará!"), Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿cuánto cuesta kinder?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response and "$10,000" in r.response
    assert "$6,450" not in r.response


@pytest.mark.asyncio
async def test_horario_emite_9_a_2_y_guard_borra_8_230() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("El horario es de 8:00 a.m. a 2:30 p.m."), Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿a qué hora entran?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "9:00 a.m. a 2:00 p.m." in r.response
    assert "8:00" not in r.response and "2:30" not in r.response


@pytest.mark.asyncio
async def test_estancias_emite_7_a_7_y_guard_borra_530() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("La estancia es hasta las 5:30 p.m."), Intent.PREGUNTA_ESTANCIAS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿tienen estancia?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "7:00 a.m. a 7:00 p.m." in r.response
    assert "5:30" not in r.response


@pytest.mark.asyncio
async def test_keyword_dispara_aunque_intent_sea_confuso() -> None:
    """Sin intent de costos (confuso_otro), la palabra 'costos' SÍ dispara la emisión."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("mmm no sé, $9,999"), Intent.CONFUSO_OTRO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="oye los costos?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response and "$9,999" not in r.response


@pytest.mark.asyncio
async def test_kinder_sin_grado_pide_grado_no_emite_horario() -> None:
    from app.core.orchestrator import procesar_turno
    from app.core.state import NivelEducativo

    repo = _Repo(_conv(NivelEducativo.KINDER, None))  # kinder, sin grado
    ctx = _leaf(repo, _Haiku("8:00 a.m. a 1:00 p.m."), Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿horario de kinder?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "grado" in r.response.lower()  # pide el grado
    assert "9:00 a.m. a 2:00 p.m." not in r.response and "8:00" not in r.response


def _conv_kinder2():
    from app.core.state import NivelEducativo
    return _conv(NivelEducativo.KINDER, "2° de Kinder")


# ============================================================
# Bloque B — guards de texto libre por el CAMINO DE PRODUCCIÓN (no mocks puros):
# Haiku devuelve venezolanismos / muchas preguntas y la respuesta final los limpia.
# ============================================================


@pytest.mark.asyncio
async def test_guard_borra_venezolanismos_camino_produccion() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    haiku = _Haiku(
        "¡Hola! ¿Está tu hijo en alguna escuela? ¿Cómo lo viven? "
        "Avísame qué día te viene bien."
    )
    ctx = _leaf(repo, haiku, Intent.SALUDO_INICIAL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="hola", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "cómo lo viven" not in low
    assert "te viene bien" not in low


@pytest.mark.asyncio
async def test_guard_tope_una_pregunta_camino_produccion() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    haiku = _Haiku("Qué gusto. ¿Vives cerca? ¿Buscas kinder? ¿Cuándo quieres venir?")
    ctx = _leaf(repo, haiku, Intent.SALUDO_INICIAL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="hola", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert r.response.count("?") <= 1


@pytest.mark.asyncio
async def test_guards_no_rompen_costos_camino_produccion() -> None:
    """Con venezolanismo + costos: el dato sigue correcto y se limpia el texto."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    haiku = _Haiku("¡Está regalado! Colegiatura: $6,450. ¿Cómo lo viven en casa?")
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿costos de kinder?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response and "$10,000" in r.response  # dato correcto intacto
    assert "$6,450" not in r.response                          # guard de cifras
    assert "regalado" not in r.response.lower()                # guard de frases
    assert "cómo lo viven" not in r.response.lower()


@pytest.mark.asyncio
async def test_costos_sin_sondeo_enganchado() -> None:
    """Punto 2: tras dar costos, NO engancha pregunta de sondeo."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    haiku = _Haiku("Colegiatura: $6,450. ¿Qué es lo que más te importa que viva tu hijo?")
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿costos de kinder?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response                       # dato correcto
    assert "más te importa" not in r.response.lower()   # sondeo eliminado
    assert "?" not in r.response                          # cero sondeo enganchado


@pytest.mark.asyncio
async def test_visita_dispara_agendado_no_sondeo() -> None:
    """Punto 1: 'quiero conocer el colegio' arranca la cita de informes, NO sondeo."""
    from app.core.appointment_flow import AppointmentHandlerResult
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv(None, None))
    haiku = _Haiku("¿Qué es lo que más te importa que tu hijo viva en la escuela?")  # sondeo
    ctx = _leaf(repo, haiku, Intent.CONFUSO_OTRO)
    ctx.append(
        patch(
            "app.core.orchestrator.handle_appointment_intent",
            AsyncMock(return_value=AppointmentHandlerResult(
                hint_para_prompt="[FLUJO AGENDADO — pide el día]",
                mensaje_coleccion="¿Qué día te queda mejor para tu visita? "
                "Atendemos lunes a viernes de 8:00 a.m. a 3:00 p.m.",
                acciones=["missing_date"],
            )),
        )
    )
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="quiero conocer el colegio", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO  # disparó agendar
    assert "qué día" in r.response.lower()                       # avanza a agendar
    assert "más te importa" not in r.response.lower()            # NO sondeo


@pytest.mark.asyncio
async def test_discovery_solo_una_pregunta_en_la_conversacion() -> None:
    """Punto 3: si ya hizo su pregunta de discovery, no hace otra."""
    from app.core.orchestrator import procesar_turno

    conv = _conv(None, None)
    conv.estado_capturado.discovery_pregunta_hecha = True  # ya gastó el cupo
    repo = _Repo(conv)
    haiku = _Haiku("Qué bien. ¿En qué año escolar va tu peque?")  # otro sondeo
    ctx = _leaf(repo, haiku, Intent.SALUDO_INICIAL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="hola", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "?" not in r.response  # cupo gastado → sin más preguntas de sondeo


@pytest.mark.asyncio
async def test_primera_discovery_marca_flag() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    haiku = _Haiku("Qué gusto que escribas. ¿Para qué ciclo buscas?")
    ctx = _leaf(repo, haiku, Intent.SALUDO_INICIAL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="hola", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert r.response.count("?") == 1  # se permite la primera
    assert repo._conv.estado_capturado.discovery_pregunta_hecha is True  # cupo marcado


@pytest.mark.asyncio
async def test_costos_sin_marcador_suelto_ni_sondeo() -> None:
    """Pulido 2+3: respuesta de costos sin '**' suelto y sin frase de sondeo."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    haiku = _Haiku(
        "** ¡Claro! Colegiatura: $6,450. Me gustaría entender qué buscas para tu hijo."
    )
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿costos?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response
    assert "** " not in r.response and not r.response.strip().endswith("**")  # sin marcador suelto
    assert "me gustaría entender" not in r.response.lower()                    # sin sondeo
    assert "$6,450" not in r.response


def test_kid_visit_no_es_cita_agendable_solo_informes() -> None:
    """Punto 4: la única cita agendable es la de informes; Kid Visit es paso posterior."""
    from app.core.prompt_builder import load_prompt_file

    rules = load_prompt_file("rules.md").lower()
    assert "única cita agendable" in rules or "cita de informes" in rules
    assert "kid visit" in rules  # se aclara que es paso POSTERIOR, no opción a elegir
    assert "posterior" in rules
