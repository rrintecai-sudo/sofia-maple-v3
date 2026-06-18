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
        nivel="kinder",
        sub_nivel="preschool",
        ciclo_escolar="2026-2027",
        inscripcion=Decimal("10000"),
        colegiatura_mensual=Decimal("5250"),
        seguro_escolar=None,
        seguro_orfandad=None,
        recursos_educativos=None,
        gastos_escolares=None,
        desayunos_snacks=None,
        talleres=None,
        cuota_graduacion=None,
        total_gastos_iniciales=None,
        num_colegiaturas=11,
        fecha_limite_pago=None,
        notas=None,
    )


def _horario_kinder2() -> HorarioResult:
    return HorarioResult(
        nivel="kinder_2",
        modalidad="regular",
        hora_inicio="09:00:00",
        hora_fin="14:00:00",
        dias="L-V",
        notas=None,
    )


_NIVELES_ALL = ["maternal", "kinder", "primaria_baja", "primaria_alta", "secundaria"]


def _estancias_kinder() -> list[EstanciaResult]:
    """Las 5 modalidades oficiales (Lili 2026-06-11). SIN After School ni Academias $630."""

    def e(nombre, ini, fin, comida, snack, aca, mes, dia, notas):
        return EstanciaResult(
            nombre=nombre,
            aplica_para=_NIVELES_ALL,
            hora_inicio=ini,
            hora_fin=fin,
            incluye_comida=comida,
            incluye_snack=snack,
            incluye_academia=aca,
            costo_mensual=Decimal(mes) if mes else None,
            costo_por_dia=Decimal(dia) if dia else None,
            inscripcion_extra=None,
            notas=notas,
        )

    return [
        e(
            "manana",
            "07:00:00",
            None,
            False,
            False,
            False,
            "550",
            None,
            "De 7:00 a.m. hasta la hora de entrada del alumno. Sin alimentos.",
        ),
        e(
            "media",
            "07:00:00",
            "16:00:00",
            True,
            False,
            True,
            "1400",
            None,
            "Incluye comida y 1 academia.",
        ),
        e(
            "completa",
            "07:00:00",
            "19:00:00",
            True,
            True,
            True,
            "2500",
            None,
            "Incluye comida, snack y 2 academias.",
        ),
        e(
            "express",
            "07:00:00",
            "19:00:00",
            True,
            False,
            False,
            None,
            "210",
            "Por día. Se solicita en recepción.",
        ),
        e(
            "academia_individual",
            None,
            None,
            True,
            False,
            False,
            "800",
            None,
            "2 clases por semana. Incluye comida los días de asistencia.",
        ),
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
        usage = types.SimpleNamespace(
            input_tokens=10,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
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
            ExtraccionTurno(),
            mensaje,
            ultimo_assistant=ultimo_assistant,
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
        patch(
            "app.core.orchestrator.get_estancias",
            AsyncMock(return_value=_estancias_kinder() if estancias is None else estancias),
        ),
    ]


def _conv(nivel, grado):
    from app.core.state import Canal, EstadoCapturado, EstadoConversacion, HijoInfo

    return EstadoConversacion(
        session_id="web:lili",
        canal=Canal.WEB,
        identificador="lili",
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
            session_id="web:lili",
            canal=None,
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
        r = await procesar_turno(
            mensaje="¿cuánto cuesta kinder?", session_id="web:lili", canal=None
        )
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
async def test_tienen_estancia_confirma_y_ofrece_sin_volcar_lista() -> None:
    """'¿tienen estancia?' (sí/no) → confirma + ofrece, SIN volcar las 5 con precios."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("..."), Intent.PREGUNTA_ESTANCIAS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿tienen estancia?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "sí" in low and "7:00 a.m. a 7:00 p.m." in r.response  # confirma + el horario
    assert "detalle" in low or "detallar" in low  # ofrece ver modalidades
    assert "$550" not in r.response and "$2,500" not in r.response  # NO volcó precios
    assert r.response.count("?") == 1  # una sola pregunta


@pytest.mark.asyncio
async def test_modalidades_estancia_oficiales_sin_afterschool_ni_academias() -> None:
    """'¿cuáles son las modalidades?' → las 5 con costos correctos, SIN After School
    ($3,100) ni Academias ($630)."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("Te cuento, $3,100 la after school."), Intent.PREGUNTA_ESTANCIAS)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="¿cuáles son las modalidades de estancia?", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)
    low = r.response.lower()
    # Las 5 con sus costos:
    assert "$550" in r.response and "$1,400" in r.response and "$2,500" in r.response
    assert "$210" in r.response and "$800" in r.response
    assert "mañana" in low and "media" in low and "completa" in low
    assert "express" in low and "academia individual" in low
    # Lo eliminado NO aparece:
    assert "$3,100" not in r.response and "after school" not in low
    assert "$630" not in r.response


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
        "¡Hola! ¿Está tu hijo en alguna escuela? ¿Cómo lo viven? Avísame qué día te viene bien."
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
    assert "$6,450" not in r.response  # guard de cifras
    assert "regalado" not in r.response.lower()  # guard de frases
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
    assert "$5,250" in r.response  # dato correcto
    assert "más te importa" not in r.response.lower()  # sondeo eliminado
    # La única pregunta permitida es la línea de cierre fija (transaccional, no sondeo).
    assert "agendamos una visita" in r.response.lower()
    assert r.response.count("?") == 1


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
            AsyncMock(
                return_value=AppointmentHandlerResult(
                    hint_para_prompt="[FLUJO AGENDADO — pide el día]",
                    mensaje_coleccion="¿Qué día te queda mejor para tu visita? "
                    "Atendemos lunes a viernes de 8:00 a.m. a 3:00 p.m.",
                    acciones=["missing_date"],
                )
            ),
        )
    )
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="quiero conocer el colegio", session_id="web:lili", canal=None
        )
        # Turno 2 (la hora) NO debe repetir la explicación de la cita de informes.
        r2 = await procesar_turno(mensaje="el jueves", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO  # disparó agendar
    # 1er turno: EXPLICA qué es la cita de informes Y pregunta el día, sin sondear.
    assert "cita de informes" in r.response.lower()
    assert "conoces las instalaciones" in r.response.lower()
    assert "qué día" in r.response.lower()
    assert "más te importa" not in r.response.lower()  # NO sondeo
    # Turno siguiente: NO repite la explicación.
    assert "cita de informes" not in r2.response.lower()


@pytest.mark.asyncio
async def test_discovery_solo_una_pregunta_en_la_conversacion() -> None:
    """Punto 3: si ya hizo su pregunta de discovery, no hace otra."""
    from app.core.orchestrator import procesar_turno

    conv = _conv(None, None)
    conv.estado_capturado.discovery_pregunta_hecha = True  # ya gastó el cupo
    repo = _Repo(conv)
    haiku = _Haiku("Qué bien. ¿En qué año escolar va tu peque?")  # otro sondeo
    # Intent que SÍ pasa por Haiku (no saludo/confuso, que ahora los reorienta el código).
    ctx = _leaf(repo, haiku, Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="ajá cuéntame", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "?" not in r.response  # cupo gastado → sin más preguntas de sondeo


@pytest.mark.asyncio
async def test_primera_discovery_marca_flag() -> None:
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    haiku = _Haiku("Qué gusto que escribas. ¿Para qué ciclo buscas?")
    ctx = _leaf(repo, haiku, Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="ajá cuéntame", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert r.response.count("?") == 1  # se permite la primera
    assert repo._conv.estado_capturado.discovery_pregunta_hecha is True  # cupo marcado


@pytest.mark.asyncio
async def test_costos_sin_marcador_suelto_ni_sondeo() -> None:
    """Pulido 2+3: respuesta de costos sin '**' suelto y sin frase de sondeo."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    haiku = _Haiku("** ¡Claro! Colegiatura: $6,450. Me gustaría entender qué buscas para tu hijo.")
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿costos?", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response
    assert "** " not in r.response and not r.response.strip().endswith("**")  # sin marcador suelto
    assert "me gustaría entender" not in r.response.lower()  # sin sondeo
    assert "$6,450" not in r.response


@pytest.mark.asyncio
async def test_quiero_informes_no_entra_agendado() -> None:
    """Bug en vivo: 'quiero informes... costos' NO debe entrar a AGENDANDO (aunque el
    LLM lo clasifique como QUIERE_AGENDAR). Da costos y se queda en exploración."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv(None, None))  # el estado se llena por extracción ("2do de kinder")
    haiku = _Haiku("¡Claro! Con gusto te comparto.")
    ctx = _leaf(repo, haiku, Intent.QUIERE_AGENDAR)  # el clasificador LLM se equivoca
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="Hola, quiero informes para kinder, mi hijo va a 2do de kinder, costos",
            session_id="web:lili",
            canal=None,
        )
    finally:
        _exit(ctx)
    # NO entró a agendar:
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.EXPLORANDO
    # dio el dato:
    assert "$5,250" in r.response
    # NO pidió el nombre del hijo:
    assert "nombre completo de tu hijo" not in r.response.lower()


@pytest.mark.asyncio
async def test_quiero_informes_para_kinder_da_costos_sin_agendar_ni_lista_rota() -> None:
    """Bug reabierto: 'quiero informes para kinder' (intent LLM pregunta_nivel) → da
    costos de kinder, NO entra a agendar, SIN lista rota '1. 2. 3.'."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv(None, None))
    # Haiku TERCO que improvisa el agendado roto. NO debe invocarse (info_directa).
    haiku = _Haiku("Perfecto, te agendo la cita de informes para Kinder.\n1. ¿Qué día?\n2.\n3.")
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="quiero informes para kinder", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.EXPLORANDO  # NO agendó
    assert "$5,250" in r.response and "$10,000" in r.response  # dio el dato
    assert "te agendo" not in r.response.lower()  # sin agendado falso
    assert "\n2.\n" not in r.response and "\n3.\n" not in r.response  # sin lista rota
    assert "1. ¿qué día" not in r.response.lower()


@pytest.mark.asyncio
async def test_quiero_conocer_los_costos_no_entra_agendado() -> None:
    """'quiero conocer los costos' = exploración (tiene 'conocer' pero pide info)."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("Va."), Intent.QUIERE_AGENDAR)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="quiero conocer los costos", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.EXPLORANDO
    assert "$5,250" in r.response


@pytest.mark.asyncio
async def test_info_directa_solo_kinder_codigo_completo() -> None:
    """'quiero informes para kinder, costos' → respuesta 100% código: solo costos de
    kinder + 1 línea de cierre. SIN saludo, SIN monólogo, SIN tabla de otros niveles."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado, NivelEducativo

    repo = _Repo(_conv(None, None))  # sin nivel; lo toma de "para kinder"
    # Haiku TERCO: saludo + monólogo + número equivocado. NO debe invocarse.
    haiku = _Haiku(
        "¡Hola! Bienvenido a Maple Collège, qué gusto. Tu hijo no solo aprende, se "
        "forma. La colegiatura es $4,900. ¿Qué es lo que más te importa?"
    )
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="quiero informes para kinder, costos", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)

    assert repo._conv.estado_capturado.nivel_buscado_actual == NivelEducativo.KINDER
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.EXPLORANDO
    # Solo kinder + cierre fijo:
    assert "$5,250" in r.response and "$10,000" in r.response
    assert "agendamos una visita" in r.response.lower()  # línea de cierre code-emitida
    # NADA de Haiku: sin saludo, sin monólogo, sin sondeo, sin número equivocado:
    assert "bienvenido a maple" not in r.response.lower()
    assert "no solo aprende" not in r.response.lower()
    assert "qué es lo que más te importa" not in r.response.lower()
    assert "$4,900" not in r.response  # ni el de maternal ni la tabla de otros niveles


@pytest.mark.asyncio
async def test_solo_nivel_etapa1_diferenciador_sin_precio() -> None:
    """FLUJO VENTA Etapa 1: el papá da SOLO el nivel → enganche (diferenciador),
    stage='valor', turnos_valor=1, SIN precio (aunque Haiku intente meterlo)."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado, NivelEducativo

    repo = _Repo(_conv(None, None))
    # Haiku TERCO que intenta meter precio → el guard de venta lo borra.
    haiku = _Haiku("Kinder es mágico. La colegiatura es $5,250 al mes. ¿Te cuento más?")
    ctx = _leaf(repo, haiku, Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="kinder", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    c = repo._conv.estado_capturado
    assert c.nivel_buscado_actual == NivelEducativo.KINDER
    assert c.fase_agendado == FaseAgendado.EXPLORANDO
    assert c.stage_venta == "valor" and c.turnos_valor == 1  # arrancó el funnel
    assert "$5,250" not in r.response and "$" not in r.response  # NUNCA precio en Etapa 1


@pytest.mark.asyncio
async def test_funnel_3_turnos_acepta_rapido_llega_al_agendado() -> None:
    """Escenario (a): nivel → continuación → continuación tras empuje → agendado."""
    from app.core.appointment_flow import AppointmentHandlerResult
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv(None, None))
    ctx = _leaf(repo, _Haiku("(redacta)"), Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="kinder", session_id="web:f", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.turnos_valor == 1

    ctx = _leaf(repo, _Haiku("(redacta)"), Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="sí, cuéntame", session_id="web:f", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.turnos_valor == 2  # llegó al umbral → empuje

    # T3: continúa tras el empuje → ACEPTA → entra al agendado (Etapa 3).
    ctx = _leaf(repo, _Haiku("(redacta)"), Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    from unittest.mock import AsyncMock, patch

    ctx.append(
        patch(
            "app.core.orchestrator.handle_appointment_intent",
            AsyncMock(
                return_value=AppointmentHandlerResult(
                    hint_para_prompt="[día]",
                    mensaje_coleccion="¿Qué día te queda mejor?",
                    acciones=["missing_date"],
                )
            ),
        )
    )
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="esta semana", session_id="web:f", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO
    assert "qué día" in r.response.lower()


@pytest.mark.asyncio
async def test_empuje_determinista_sin_descubrimiento() -> None:
    """Bug 1: en el empuje, Haiku terco mete edad/'¿qué te importa?' → el código las
    suprime y cierra SIEMPRE con el empuje determinístico."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    ctx = _leaf(repo, _Haiku("(valor)"), Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="kinder", session_id="web:e", canal=None)
    finally:
        _exit(ctx)
    # Empuje: Haiku TERCO con discovery → se borra; el push lo pone el código.
    haiku = _Haiku("Un día en Kinder es mágico. ¿Qué edad tiene? ¿Qué es lo que te importa?")
    ctx = _leaf(repo, haiku, Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="sí", session_id="web:e", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "esta semana o la siguiente" in low  # empuje determinístico
    assert "qué edad" not in low and "te importa" not in low  # discovery suprimido
    assert r.response.count("?") == 1  # una sola pregunta (el empuje)


@pytest.mark.asyncio
async def test_horario_primaria_no_dice_kinder() -> None:
    """Bug 2: el papá dijo Primaria → la rama de horarios NO debe decir 'Kinder'."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import NivelEducativo

    repo = _Repo(_conv(NivelEducativo.PRIMARIA, None))  # primaria sin grado
    ctx = _leaf(repo, _Haiku("(x)"), Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿y el horario?", session_id="web:h", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "primaria" in low and "kinder" not in low  # respeta el nivel guardado
    assert "grado" in low  # pide el grado de primaria


@pytest.mark.asyncio
async def test_horario_grado_suelto_resuelve_sin_loop() -> None:
    """Bug: 'primaria → ¿qué grado? → 3' debe resolver el horario, no repetir la
    pregunta. '3' fija 3° de Primaria y re-emite el horario."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import NivelEducativo

    repo = _Repo(_conv(NivelEducativo.PRIMARIA, None))
    ctx = _leaf(repo, _Haiku("(x)"), Intent.PREGUNTA_HORARIO)
    _enter(ctx)
    try:
        r1 = await procesar_turno(mensaje="¿y el horario?", session_id="web:g", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.pendiente_grado_horario is True
    assert "grado" in r1.response.lower()

    # "3" suelto → resuelve el grado y emite un HORARIO (no repite la pregunta).
    ctx = _leaf(repo, _Haiku("(x)"), Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        r2 = await procesar_turno(mensaje="3", session_id="web:g", canal=None)
    finally:
        _exit(ctx)
    c = repo._conv.estado_capturado
    assert c.pendiente_grado_horario is False
    assert c.hijos[0].grado == "3° de Primaria"
    assert "🕐" in r2.response and "grado" not in r2.response.lower()  # horario, no re-pregunta


def test_extraer_grado_suelto_variantes() -> None:
    from app.core.oferta_resolver import extraer_grado_suelto
    from app.core.state import NivelEducativo

    p = NivelEducativo.PRIMARIA
    assert extraer_grado_suelto("3", p) == "3° de Primaria"
    assert extraer_grado_suelto("4to", p) == "4° de Primaria"
    assert extraer_grado_suelto("tercero", p) == "3° de Primaria"
    assert extraer_grado_suelto("1 a 3", p) == "1° de Primaria"
    assert extraer_grado_suelto("4 a 6", p) == "4° de Primaria"
    assert extraer_grado_suelto("9", p) is None  # fuera de rango
    assert extraer_grado_suelto("esta semana", p) is None


@pytest.mark.asyncio
async def test_quiere_persona_responde_calido_sin_menu() -> None:
    """Bug 2: 'no quiero un robot' → respuesta cálida (Sofía/admisiones), sin menú."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    ctx = _leaf(repo, _Haiku("(x)"), Intent.CONFUSO_OTRO)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="puedo hablar con otra persona y no con un robot",
            session_id="web:p",
            canal=None,
        )
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "sofía" in low and "admisiones" in low
    assert "puedo contarte de los niveles" not in low  # NO el menú robótico


@pytest.mark.asyncio
async def test_anti_duplicado_no_repite_identico() -> None:
    """Bug 1: dos turnos que darían el MISMO menú → el 2º varía."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    ctx = _leaf(repo, _Haiku("(x)"), Intent.CONFUSO_OTRO)
    _enter(ctx)
    try:
        r1 = await procesar_turno(mensaje="mmm", session_id="web:d", canal=None)
        r2 = await procesar_turno(mensaje="ehh", session_id="web:d", canal=None)
    finally:
        _exit(ctx)
    assert r1.response != r2.response  # no idéntico


@pytest.mark.asyncio
async def test_horario_extendido_solo_estancias_no_escolar() -> None:
    """Bug 4: 'horario extendido' = estancias; NO mezcla el horario escolar."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_kinder2())
    ctx = _leaf(repo, _Haiku("(x)"), Intent.PREGUNTA_ESTANCIAS)
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="háblame del horario extendido", session_id="web:he", canal=None
        )
    finally:
        _exit(ctx)
    assert "🏫" in r.response  # estancias
    assert "las clases son de" not in r.response.lower()  # NO el horario escolar


def test_funnel_usa_contenido_por_grado() -> None:
    """Bugs 5/6 (refactor verbatim): '2° de Kinder' → el hint inyecta el texto EXACTO
    del grado tomado del KB oficial (no beats congelados)."""
    from app.core.sales_funnel import _kb_contenido, decidir_funnel
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.KINDER,
        hijos=[HijoInfo(nivel=NivelEducativo.KINDER, grado="2° de Kinder")],
    )
    d = decidir_funnel(
        capt,
        es_continuacion=False,
        nivel_en_msg="kinder",
        pide_info_nueva=False,
        en_agendado=False,
        umbral=2,
    )
    assert "2° de Kinder" in d.hint
    # El hint trae el texto VERBATIM del grado desde el KB oficial (no genérico).
    por_grado, _ = _kb_contenido()
    texto_2k = por_grado.get("2° de kinder", "")
    assert texto_2k and texto_2k in d.hint


def _conv_agendando_secundaria():
    from app.core.state import FaseAgendado, NivelEducativo

    c = _conv(NivelEducativo.SECUNDARIA, "1° de Secundaria")
    c.estado_capturado.fase_agendado = FaseAgendado.AGENDANDO
    c.estado_capturado.opciones_dia_propuestas = ["2026-06-17", "2026-06-18", "2026-06-19"]
    return c


@pytest.mark.asyncio
async def test_pausa_info_en_agendado_contenido_grado_no_avanza_a_hora() -> None:
    """T5 REAL: 'Que se fortalece en primero de secundaria' (pregunta de contenido) en
    el paso del día NO se trata como fecha; PAUSA, responde contenido y re-ofrece. NO
    avanza a hora; 'primero' NO matchea el ordinal (frase de grado)."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv_agendando_secundaria())
    ctx = _leaf(
        repo, _Haiku("Contenido de secundaria redactado."), Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO
    )
    _enter(ctx)
    try:
        r = await procesar_turno(
            mensaje="Que se fortalece en primero de secundaria", session_id="web:p5", canal=None
        )
    finally:
        _exit(ctx)
    c = repo._conv.estado_capturado
    assert c.cita_fecha_slot is None and c.cita_hora_slot is None  # NO avanzó
    assert c.fase_agendado == FaseAgendado.AGENDANDO  # sigue la cita en proceso
    assert "seguimos con tu visita" in r.response.lower()  # re-oferta de la visita


@pytest.mark.asyncio
async def test_pregunta_contenido_tras_empuje_no_acepta_la_visita() -> None:
    """EL BUG REAL (sesión baa48e3d): tras el empuje (stage=valor, tv=2), 'que se
    fortalece?' NO debe tomarse como aceptar la visita ni explicar la cita: responde
    contenido del grado + re-oferta, y NO entra a AGENDANDO."""
    from app.core.orchestrator import procesar_turno
    from app.core.state import FaseAgendado

    repo = _Repo(_conv(None, None))
    # T0 nivel secundaria → Etapa 1
    ctx = _leaf(repo, _Haiku("(contenido)"), Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="primero de secundaria", session_id="web:bug", canal=None)
    finally:
        _exit(ctx)
    # T1 "si" → empuje (tv=2)
    ctx = _leaf(repo, _Haiku("(contenido)"), Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="si", session_id="web:bug", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.turnos_valor == 2
    # T2 "que se fortalece?" → contenido + re-oferta, NO entra a agendado
    ctx = _leaf(
        repo, _Haiku("En secundaria se fortalece el pensamiento crítico…"), Intent.CONFUSO_OTRO
    )
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="que se fortalece?", session_id="web:bug", canal=None)
    finally:
        _exit(ctx)
    c = repo._conv.estado_capturado
    assert c.fase_agendado == FaseAgendado.EXPLORANDO  # NO entró a agendado
    assert "cita de informes" not in r.response.lower()  # NO explicó la cita
    assert "visita" in r.response.lower()  # re-ofreció la visita


@pytest.mark.asyncio
async def test_pausa_contenido_sin_nivel_en_msg_usa_nivel_estado() -> None:
    """T4: 'Que se fortalece' (sin nivel en el mensaje) usa nivel_buscado_actual del
    estado para responder contenido, NO 'no te entendí'."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_agendando_secundaria())
    ctx = _leaf(
        repo, _Haiku("En secundaria se fortalece el pensamiento crítico…"), Intent.CONFUSO_OTRO
    )
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="Que se fortalece", session_id="web:p4", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "no te entendí" not in low
    assert "seguimos con tu visita" in low  # respondió contenido + re-oferta


@pytest.mark.asyncio
async def test_pausa_costos_en_agendado_da_dato_y_reoferta() -> None:
    """Costos en agendado (paso día) → pausa: emite el dato + re-oferta de fechas."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv_agendando_secundaria())
    ctx = _leaf(repo, _Haiku("(x)"), Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿cuánto cuesta?", session_id="web:pc", canal=None)
    finally:
        _exit(ctx)
    assert "$5,250" in r.response and "💰" in r.response
    assert "seguimos con tu visita" in r.response.lower()
    assert repo._conv.estado_capturado.cita_fecha_slot is None  # no avanzó


def test_secundaria_grado_en_contenido() -> None:
    """1° de Secundaria YA tiene beats por grado (no genérico)."""
    from app.core.sales_funnel import _BEATS, _beats_de

    for g in (
        "1° de Secundaria",
        "2° de Secundaria",
        "3° de Secundaria",
        "2° de Primaria",
        "3° de Primaria",
    ):
        assert _BEATS.get(g), f"{g} sin beats"
    beats_1sec = _beats_de("1° de Secundaria", "secundaria")
    assert len(beats_1sec) >= 4
    # Facetas DISTINTAS: pensamiento crítico, proyectos, autonomía, emocional, liderazgo.
    blob = " ".join(beats_1sec).lower()
    assert "pensamiento crítico" in blob and "proyectos" in blob and "liderazgo" in blob


def test_recorte_cap_4_oraciones() -> None:
    """Ajuste 1 (LONGITUD): el cap REAL recorta a 4 oraciones COMPLETAS, sin cortar
    a media frase. Mensajes de 5+ frases nunca llegan al papá."""
    from app.core.output_guards import recortar_oraciones

    largo = "Una. Dos! ¿Tres? Cuatro. Cinco. Seis."
    out = recortar_oraciones(largo, maximo=4)
    assert out == "Una. Dos! ¿Tres? Cuatro."
    # 4 o menos: intacto, no corta a media frase.
    assert recortar_oraciones("Una. Dos.", 4) == "Una. Dos."


def test_beat_diferenciador_siempre_en_enganche() -> None:
    """Ajuste 2 — CONDICIÓN 1: el beat del diferenciador ('se forma') SIEMPRE va en
    el enganche, aunque la rotación de beats no usados elija otros."""
    from app.core.sales_funnel import decidir_funnel
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.SECUNDARIA,
        hijos=[HijoInfo(nivel=NivelEducativo.SECUNDARIA, grado="1° de Secundaria")],
    )
    d = decidir_funnel(
        capt,
        es_continuacion=False,
        nivel_en_msg="secundaria",
        pide_info_nueva=False,
        en_agendado=False,
        umbral=2,
        beats_usados=[],
    )
    assert "se forma" in d.hint  # diferenciador presente en el enganche


def test_segundo_turno_no_repite_beat() -> None:
    """Ajuste 2 — CONDICIÓN 2: el segundo turno de contenido NO repite el beat del
    primero (beats_venta_usados compartido entre funnel y pausa de contenido)."""
    from app.core.sales_funnel import decidir_funnel, hint_contenido
    from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo

    capt = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.SECUNDARIA,
        hijos=[HijoInfo(nivel=NivelEducativo.SECUNDARIA, grado="1° de Secundaria")],
    )
    d1 = decidir_funnel(
        capt,
        es_continuacion=False,
        nivel_en_msg="secundaria",
        pide_info_nueva=False,
        en_agendado=False,
        umbral=2,
        beats_usados=[],
    )
    usados = list(d1.beats_usados or [])
    # Camino funnel (Etapa 2)
    capt.stage_venta, capt.turnos_valor = "valor", 1
    d2 = decidir_funnel(
        capt,
        es_continuacion=True,
        nivel_en_msg=None,
        pide_info_nueva=False,
        en_agendado=False,
        umbral=2,
        beats_usados=usados,
    )
    assert not (set(usados) & set(d2.beats_usados or [])), "Etapa 2 repitió beat"
    # Camino pausa de contenido (hint_contenido) comparte beats_usados
    usados += d2.beats_usados or []
    _, beats_c = hint_contenido("secundaria", "1° de Secundaria", usados)
    assert not (set(usados) & set(beats_c)), "pausa de contenido repitió beat"


def test_contenido_grado_siempre_inyecta_verbatim() -> None:
    """Refactor verbatim: el contenido por grado ya NO depende de beats discretos —
    hint_contenido SIEMPRE inyecta el texto EXACTO del grado desde el KB (no degrada a
    None ni a mensaje vacío), aun pasando un `usados` lleno. El segundo elemento queda
    [] por compatibilidad con el caller."""
    from app.core.sales_funnel import _kb_contenido, hint_contenido

    hint, beats = hint_contenido("secundaria", "1° de Secundaria", ["lo que sea"])
    por_grado, _ = _kb_contenido()
    texto = por_grado.get("1° de secundaria", "")
    assert texto and texto in hint
    assert beats == []


def test_beats_facetas_distintas_sin_cruce_grado_nivel() -> None:
    """Ajuste 2 (b): los beats de cada grado son únicos (sin duplicados internos) y NO
    se cruzan con su fallback de nivel — así rotar nunca recae en la misma idea (el bug
    era 'argumenta con criterio' presente en grado Y en nivel)."""
    from app.core.sales_funnel import _BEATS, _BEATS_NIVEL

    nivel_de = {
        "Kinder": "kinder",
        "Primaria": "primaria",
        "Secundaria": "secundaria",
    }
    for grado, beats in _BEATS.items():
        assert len(beats) == len(set(beats)), f"{grado} tiene beats duplicados"
        nivel = next((v for k, v in nivel_de.items() if k in grado), None)
        fallback = set(_BEATS_NIVEL.get(nivel, []))
        assert not (set(beats) & fallback), f"{grado} cruza con fallback {nivel}"


def test_etiqueta_hoy_manana_en_paso_hora() -> None:
    """Defecto 2: el paso de HORA etiqueta hoy/mañana en el día (fecha_humana_solo_dia
    con now), igual que la propuesta de días. Antes decía 'miércoles 17' sin 'hoy'."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.core.appointment_extractor import fecha_humana_solo_dia

    mty = ZoneInfo("America/Monterrey")
    now = datetime(2026, 6, 17, 9, 0, tzinfo=mty)  # miércoles 17
    assert fecha_humana_solo_dia("2026-06-17", now) == "hoy, miércoles 17 de junio"
    assert fecha_humana_solo_dia("2026-06-18", now) == "mañana, jueves 18 de junio"
    assert fecha_humana_solo_dia("2026-06-25", now) == "jueves 25 de junio"
    # Sin now: comportamiento previo intacto (sin etiqueta).
    assert fecha_humana_solo_dia("2026-06-17") == "miércoles 17 de junio"


def test_concordancia_de_hoy_no_del_hoy() -> None:
    """Defecto 1: 'del hoy' es agramatical. Con etiqueta hoy/mañana, la pregunta de hora
    usa 'de hoy,…' y deja caer el artículo ('hoy,…' no 'el hoy,…')."""
    from app.core.appointment_messages import art_dia, prep_dia, render_pregunta_campo

    assert prep_dia("hoy, miércoles 17 de junio") == "de"
    assert prep_dia("mañana, jueves 18 de junio") == "de"
    assert prep_dia("jueves 25 de junio") == "del"
    assert art_dia("hoy, miércoles 17") == "" and art_dia("jueves 25") == "el "
    # Mensaje code-emitido (no redactado por Haiku) debe leer natural.
    msg = render_pregunta_campo("hora", dia="hoy, miércoles 17 de junio")
    assert "de hoy, miércoles 17" in msg and "del hoy" not in msg
    msg2 = render_pregunta_campo("hora", dia="jueves 25 de junio")
    assert "del jueves 25" in msg2


def test_beats_sin_patron_etiqueta_dos_puntos() -> None:
    """Defecto 2 (Gaby 3): ningún beat lleva ':' — el patrón 'Etiqueta: lista' hacía que
    Haiku escupiera 'La parte emocional:' / 'El liderazgo:'."""
    from app.core.sales_funnel import _BEATS, _BEATS_NIVEL

    for fuente in (_BEATS, _BEATS_NIVEL):
        for grado, beats in fuente.items():
            for b in beats:
                assert ":" not in b, f"{grado} tiene beat con ':' → {b!r}"


def test_recap_nombra_facetas_vistas() -> None:
    """Defecto 3: al agotar beats, el recap NOMBRA las facetas ya vistas (reconoce la
    pregunta) en vez de saltar directo a la re-oferta. None si no hay nada que nombrar."""
    from app.core.sales_funnel import _beats_de, recap_beats_vistos

    recap = recap_beats_vistos(_beats_de("1° de Secundaria", "secundaria"))
    assert recap and recap.startswith("Ya te conté ")
    # Nombra facetas reconocibles del grado.
    assert "pensamiento crítico" in recap or "proyectos" in recap
    assert recap_beats_vistos([]) is None


def test_etiqueta_hoy_manana_en_confirmacion() -> None:
    """Ajuste 3 (Gaby 9): la confirmación etiqueta 'hoy'/'mañana' según `now` en hora
    de Saltillo (America/Monterrey)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.core.appointment_messages import render_registration_message

    mty = ZoneInfo("America/Monterrey")
    now = datetime(2026, 6, 17, 9, 0, tzinfo=mty)  # miércoles 17
    hoy = render_registration_message(
        fecha_hora=datetime(2026, 6, 17, 11, 0, tzinfo=mty), campus=None, canal="web", now=now
    )
    assert "📅 Día: hoy, miércoles 17" in hoy
    manana = render_registration_message(
        fecha_hora=datetime(2026, 6, 18, 11, 0, tzinfo=mty), campus=None, canal="web", now=now
    )
    assert "📅 Día: mañana, jueves 18" in manana
    # fecha lejana: sin etiqueta
    lejos = render_registration_message(
        fecha_hora=datetime(2026, 6, 25, 11, 0, tzinfo=mty), campus=None, canal="web", now=now
    )
    assert "📅 Día: jueves 25" in lejos and "hoy" not in lejos.split("\n")[2]


def test_gate_no_repregunta_edad_si_ya_esta() -> None:
    """Bug 3: edad capturada (en el funnel) → el gate del agendado NO la repregunta."""
    from app.core.appointment_flow import datos_lead_faltantes
    from app.core.state import HijoInfo, NivelEducativo

    conv = _conv(NivelEducativo.PRIMARIA, None)
    conv.estado_capturado.hijos = [
        HijoInfo(nivel=NivelEducativo.PRIMARIA, edad=6, grado="1° de Primaria")
    ]
    faltantes = datos_lead_faltantes(conv)
    assert "edad del hijo" not in faltantes
    assert "grado escolar del hijo" not in faltantes


@pytest.mark.asyncio
async def test_funnel_precio_a_media_charla_pausa_y_no_rompe() -> None:
    """Escenario (c): nivel → preguntan precio → da el precio y el contador NO sube."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    ctx = _leaf(repo, _Haiku("(redacta)"), Intent.PREGUNTA_NIVEL)
    _enter(ctx)
    try:
        await procesar_turno(mensaje="kinder", session_id="web:c", canal=None)
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.turnos_valor == 1

    ctx = _leaf(repo, _Haiku("(redacta)"), Intent.PREGUNTA_COSTOS)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="¿y los costos?", session_id="web:c", canal=None)
    finally:
        _exit(ctx)
    # Pausa: el contador NO sube, y da el precio correcto (info_directa).
    assert repo._conv.estado_capturado.turnos_valor == 1
    assert "$5,250" in r.response


@pytest.mark.asyncio
async def test_saludo_repetido_reorienta_sin_estoy_por_aca() -> None:
    """Saludo REPETIDO (ya hubo turno) → reorienta a pedir nivel, no rebota seco."""
    from app.core.orchestrator import procesar_turno

    conv = _conv(None, None)
    repo = _Repo(conv)
    repo._messages.append({"role": "assistant", "content": "¡Hola!"})  # ya no es nueva
    haiku = _Haiku("¡Hey! 👋 Estoy por acá.")  # rebote seco — se bypassa
    ctx = _leaf(repo, haiku, Intent.SALUDO_INICIAL)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="Hola", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "nivel" in low and "?" in r.response  # reorienta a pedir nivel
    assert "estoy por acá" not in low  # sin rebote seco


@pytest.mark.asyncio
async def test_confuso_sin_datos_reorienta() -> None:
    """Confuso sin consulta ni datos → línea de reorientación útil (no Haiku suelto)."""
    from app.core.orchestrator import procesar_turno

    repo = _Repo(_conv(None, None))
    haiku = _Haiku("ehh...")  # Haiku suelto — se bypassa
    ctx = _leaf(repo, haiku, Intent.CONFUSO_OTRO)
    _enter(ctx)
    try:
        r = await procesar_turno(mensaje="mmm pues no sé", session_id="web:lili", canal=None)
    finally:
        _exit(ctx)
    low = r.response.lower()
    assert "niveles" in low and "costos" in low and "visita" in low
    assert "ehh" not in low


def test_kid_visit_no_es_cita_agendable_solo_informes() -> None:
    """Punto 4: la única cita agendable es la de informes; Kid Visit es paso posterior."""
    from app.core.prompt_builder import load_prompt_file

    rules = load_prompt_file("rules.md").lower()
    assert "única cita agendable" in rules or "cita de informes" in rules
    assert "kid visit" in rules  # se aclara que es paso POSTERIOR, no opción a elegir
    assert "posterior" in rules
