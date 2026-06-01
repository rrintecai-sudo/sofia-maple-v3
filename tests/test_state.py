"""Tests de los modelos de estado."""

from __future__ import annotations

from datetime import datetime

import pytest
from app.core.state import (
    Canal,
    ClasificacionLead,
    EstadoCapturado,
    EstadoConversacion,
    FaseJourney,
    HijoInfo,
    Modo,
    NivelEducativo,
)


def test_hijo_efectivo_fusiona_huerfano() -> None:
    """FIX (d): un hijo huérfano {edad:4} + el real {nombre,nivel,grado} → uno solo."""
    capt = EstadoCapturado(
        hijos=[
            HijoInfo(edad=4),  # huérfano de sesión contaminada
            HijoInfo(nombre="Emanuel", nivel=NivelEducativo.KINDER, grado="2° de Kinder"),
        ]
    )
    h = capt.hijo_efectivo()
    assert h is not None
    assert h.nombre == "Emanuel"
    assert h.edad == 4
    assert h.nivel == NivelEducativo.KINDER
    assert h.grado == "2° de Kinder"


def test_hijo_efectivo_vacio_devuelve_none() -> None:
    assert EstadoCapturado().hijo_efectivo() is None


def test_hijo_efectivo_no_muta_la_lista() -> None:
    capt = EstadoCapturado(hijos=[HijoInfo(edad=4), HijoInfo(nombre="Ana")])
    _ = capt.hijo_efectivo()
    assert len(capt.hijos) == 2  # la lista persistida no se toca
    assert capt.hijos[0].nombre is None


def test_estado_conversacion_nueva_whatsapp() -> None:
    """`nueva()` parsea session_id con prefijo canal."""
    estado = EstadoConversacion.nueva("whatsapp:5218441302112@s.whatsapp.net")
    assert estado.canal == Canal.WHATSAPP
    assert estado.identificador == "5218441302112@s.whatsapp.net"
    assert estado.fase_journey == FaseJourney.BIENVENIDA
    assert estado.modo == Modo.NORMAL
    assert estado.agendado is False
    assert estado.estado_capturado.hijos == []


def test_estado_conversacion_nueva_telegram() -> None:
    estado = EstadoConversacion.nueva("telegram:123456789")
    assert estado.canal == Canal.TELEGRAM
    assert estado.identificador == "123456789"


def test_estado_conversacion_nueva_web() -> None:
    estado = EstadoConversacion.nueva("web:abc-def-uuid")
    assert estado.canal == Canal.WEB
    assert estado.identificador == "abc-def-uuid"


def test_estado_conversacion_nueva_sin_prefijo_falla() -> None:
    with pytest.raises(ValueError, match="prefijo de canal"):
        EstadoConversacion.nueva("5218441302112")


def test_estado_conversacion_nueva_canal_invalido_falla() -> None:
    with pytest.raises(ValueError, match="prefijo de canal"):
        EstadoConversacion.nueva("signal:12345")


def test_marcar_frase_usada_no_duplica() -> None:
    estado = EstadoConversacion.nueva("web:test")
    frase = "Aquí trabajamos muy de la mano con las familias"
    estado.marcar_frase_usada(frase)
    estado.marcar_frase_usada(frase)
    assert estado.frases_usadas == [frase]


def test_marcar_agendado_actualiza_estado() -> None:
    estado = EstadoConversacion.nueva("web:test")
    fecha = datetime(2026, 5, 20, 10, 0)
    estado.marcar_agendado(fecha=fecha, campus="Campus 1")
    assert estado.agendado is True
    assert estado.fecha_agendado == fecha
    assert estado.estado_capturado.cita_agendada is True
    assert estado.estado_capturado.campus_cita == "Campus 1"
    assert estado.fase_journey == FaseJourney.POST_AGENDADO


def test_estado_capturado_conocemos_string() -> None:
    capt = EstadoCapturado(nombre_papa="Juan")
    assert capt.conocemos("nombre_papa") is True
    assert capt.conocemos("nombre_papa") is True
    assert capt.conocemos("telefono") is False


def test_estado_capturado_conocemos_list() -> None:
    capt = EstadoCapturado()
    assert capt.conocemos("hijos") is False
    capt.hijos.append(HijoInfo(nombre="Mateo", edad=7))
    assert capt.conocemos("hijos") is True


def test_estado_capturado_conocemos_bool() -> None:
    """Para flags booleanos, solo True cuenta como conocido."""
    capt = EstadoCapturado()
    assert capt.conocemos("pidio_costos") is False
    capt.pidio_costos = True
    assert capt.conocemos("pidio_costos") is True


def test_hijo_info_edad_validation() -> None:
    """Edad debe estar 0..20."""
    HijoInfo(nombre="Mateo", edad=0)
    HijoInfo(nombre="Mateo", edad=20)
    with pytest.raises(ValueError):
        HijoInfo(nombre="Mateo", edad=-1)
    with pytest.raises(ValueError):
        HijoInfo(nombre="Mateo", edad=21)


def test_nivel_educativo_enum() -> None:
    assert NivelEducativo.MATERNAL.value == "maternal"
    assert NivelEducativo.PRIMARIA.value == "primaria"


def test_clasificacion_default() -> None:
    capt = EstadoCapturado()
    assert capt.clasificacion == ClasificacionLead.SIN_CLASIFICAR
