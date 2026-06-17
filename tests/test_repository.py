"""Tests del repository (mapping helpers + mocked PostgREST con respx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from app.config import Settings
from app.core.repository import (
    Repository,
    _estado_to_row,
    _row_to_estado,
)
from app.core.state import (
    Canal,
    EstadoCapturado,
    EstadoConversacion,
    FaseJourney,
    HijoInfo,
    Modo,
    NivelEducativo,
)


def _make_settings() -> Settings:
    return Settings(
        supabase_url="https://x.supabase.co",
        supabase_service_key="sk-test",
    )


def test_estado_to_row_basic() -> None:
    estado = EstadoConversacion.nueva("web:abc")
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    row = _estado_to_row(estado)
    assert row["session_id"] == "web:abc"
    assert row["canal"] == "web"
    assert row["identificador"] == "abc"
    assert row["fase_journey"] == "descubrimiento"
    assert row["modo"] == "normal"
    assert row["agendado"] is False
    assert row["estado_capturado"] == {
        "hijos": [],
        "miedos": [],
        "resono_con": [],
        "objeciones_planteadas": [],
        "costos_compartidos_niveles": [],
        "presupuesto_mencionado": False,
        "pidio_costos": False,
        "cita_agendada": False,
        "fase_agendado": "explorando",
        "cita_fecha_slot": None,
        "cita_hora_slot": None,
        "opciones_dia_propuestas": [],
        "ultimo_campo_pedido": None,
        "discovery_pregunta_hecha": False,
        "pendiente_grado_horario": False,
        "stage_venta": "enganche",
        "turnos_valor": 0,
        "beats_venta_usados": [],
        "handoff_a_lily": False,
        "vive_fuera_saltillo": False,
        "clasificacion": "sin_clasificar",
        "nombre_papa": None,
        "telefono": None,
        "email_papa": None,
        "nivel_buscado_actual": None,
        "fecha_cita": None,
        "campus_cita": None,
        "fuente_entrada": None,
    }


def test_row_to_estado_basic() -> None:
    row = {
        "session_id": "telegram:12345",
        "canal": "telegram",
        "identificador": "12345",
        "estado_capturado": {
            "nombre_papa": "Juan",
            "hijos": [{"nombre": "Mateo", "edad": 8, "nivel": "primaria"}],
        },
        "frases_usadas": ["frase a"],
        "fase_journey": "informacion",
        "agendado": True,
        "fecha_agendado": None,
        "modo": "normal",
        "notas_internas": None,
        "tester": False,
    }
    estado = _row_to_estado(row)
    assert estado.canal == Canal.TELEGRAM
    assert estado.fase_journey == FaseJourney.INFORMACION
    assert estado.agendado is True
    assert estado.estado_capturado.nombre_papa == "Juan"
    assert len(estado.estado_capturado.hijos) == 1
    assert estado.estado_capturado.hijos[0].nivel == NivelEducativo.PRIMARIA


def test_row_to_estado_handles_invalid_enums() -> None:
    """Si fase/modo vienen con valores inválidos, hace fallback a defaults."""
    row = {
        "session_id": "web:x",
        "canal": "web",
        "identificador": "x",
        "estado_capturado": {},
        "frases_usadas": [],
        "fase_journey": "inventada_xyz",
        "agendado": False,
        "fecha_agendado": None,
        "modo": "rar0",
        "notas_internas": None,
        "tester": False,
    }
    estado = _row_to_estado(row)
    assert estado.fase_journey == FaseJourney.BIENVENIDA  # fallback
    assert estado.modo == Modo.NORMAL  # fallback


def test_roundtrip_estado_to_row_to_estado() -> None:
    original = EstadoConversacion.nueva("web:rt")
    original.estado_capturado = EstadoCapturado(
        nombre_papa="Ana",
        hijos=[HijoInfo(nombre="Lía", edad=5, nivel=NivelEducativo.KINDER)],
        miedos=["bullying"],
        pidio_costos=True,
    )
    original.fase_journey = FaseJourney.EDUCACION
    original.frases_usadas = ["frase 1"]

    row = _estado_to_row(original)
    rebuilt = _row_to_estado(row)

    assert rebuilt.canal == original.canal
    assert rebuilt.fase_journey == original.fase_journey
    assert rebuilt.estado_capturado.nombre_papa == "Ana"
    assert rebuilt.estado_capturado.hijos[0].nombre == "Lía"
    assert rebuilt.estado_capturado.pidio_costos is True
    assert rebuilt.frases_usadas == ["frase 1"]


@pytest.mark.asyncio
@respx.mock
async def test_get_conversation_returns_none_when_not_found() -> None:
    respx.get("https://x.supabase.co/rest/v1/sofia_conversations").mock(
        return_value=httpx.Response(200, json=[])
    )
    repo = Repository(settings=_make_settings())
    result = await repo.get_conversation("web:nope")
    assert result is None
    await repo.close()


@pytest.mark.asyncio
@respx.mock
async def test_insert_message_returns_id() -> None:
    respx.post("https://x.supabase.co/rest/v1/sofia_messages").mock(
        return_value=httpx.Response(201, json=[{"id": 42}])
    )
    repo = Repository(settings=_make_settings())
    msg_id = await repo.insert_message(
        session_id="web:test",
        role="user",
        content="hola",
    )
    assert msg_id == 42
    await repo.close()
