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


_NIVELES_ALL = ["maternal", "kinder", "primaria_baja", "primaria_alta", "secundaria"]


def _estancias_kinder() -> list[EstanciaResult]:
    """Las 5 modalidades oficiales (Lili 2026-06-11). SIN After School ni Academias $630."""
    def e(nombre, ini, fin, comida, snack, aca, mes, dia, notas):
        return EstanciaResult(
            nombre=nombre, aplica_para=_NIVELES_ALL, hora_inicio=ini, hora_fin=fin,
            incluye_comida=comida, incluye_snack=snack, incluye_academia=aca,
            costo_mensual=Decimal(mes) if mes else None,
            costo_por_dia=Decimal(dia) if dia else None, inscripcion_extra=None, notas=notas,
        )
    return [
        e("manana", "07:00:00", None, False, False, False, "550", None,
          "De 7:00 a.m. hasta la hora de entrada del alumno. Sin alimentos."),
        e("media", "07:00:00", "16:00:00", True, False, True, "1400", None,
          "Incluye comida y 1 academia."),
        e("completa", "07:00:00", "19:00:00", True, True, True, "2500", None,
          "Incluye comida, snack y 2 academias."),
        e("express", "07:00:00", "19:00:00", True, False, False, None, "210",
          "Por día. Se solicita en recepción."),
        e("academia_individual", None, None, True, False, False, "800", None,
          "2 clases por semana. Incluye comida los días de asistencia."),
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
    assert "detalle" in low or "detallar" in low                  # ofrece ver modalidades
    assert "$550" not in r.response and "$2,500" not in r.response  # NO volcó precios
    assert r.response.count("?") == 1                              # una sola pregunta


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
        # Turno 2 (la hora) NO debe repetir la explicación de la cita de informes.
        r2 = await procesar_turno(
            mensaje="el jueves", session_id="web:lili", canal=None
        )
    finally:
        _exit(ctx)
    assert repo._conv.estado_capturado.fase_agendado == FaseAgendado.AGENDANDO  # disparó agendar
    # 1er turno: EXPLICA qué es la cita de informes Y pregunta el día, sin sondear.
    assert "cita de informes" in r.response.lower()
    assert "conoces las instalaciones" in r.response.lower()
    assert "qué día" in r.response.lower()
    assert "más te importa" not in r.response.lower()            # NO sondeo
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
            session_id="web:lili", canal=None,
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
    assert "$5,250" in r.response and "$10,000" in r.response                    # dio el dato
    assert "te agendo" not in r.response.lower()                                 # sin agendado falso
    assert "\n2.\n" not in r.response and "\n3.\n" not in r.response             # sin lista rota
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


def test_kid_visit_no_es_cita_agendable_solo_informes() -> None:
    """Punto 4: la única cita agendable es la de informes; Kid Visit es paso posterior."""
    from app.core.prompt_builder import load_prompt_file

    rules = load_prompt_file("rules.md").lower()
    assert "única cita agendable" in rules or "cita de informes" in rules
    assert "kid visit" in rules  # se aclara que es paso POSTERIOR, no opción a elegir
    assert "posterior" in rules
