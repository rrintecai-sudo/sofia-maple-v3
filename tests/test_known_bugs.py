"""Tests de regresión por bugs específicos detectados en producción de Sofía v1.

Cada test reproduce el escenario que falló históricamente y verifica que la
arquitectura nueva (validators + estado capturado + prompt modular) NO lo
permite. Estos tests son DETERMINÍSTICOS — no llaman LLMs, simulan respuestas
problemáticas y verifican que los validators las detectan/bloquean.

Bugs cubiertos (referencias a SOFIA_AUDIT.md C4):
1. Siembra de alianza repetida en el mismo chat
2. Pregunta repetida tras conocer escuela actual
3. Pregunta repetida tras conocer nivel
4. Envío fantasma de imagen sin llamar al tool
5. Evasión de pregunta directa de costos
"""

from __future__ import annotations

from app.core.intent_classifier import Intent
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo
from app.core.validators import (
    extraer_frases_municion_usadas,
    run_all_validators,
)


def test_bug_alianza_repetida_es_detectada() -> None:
    """Producción 13-may: Sofía sembró 'trabajamos muy de la mano con las familias' 2 veces.

    Si la respuesta repite una frase munición ya marcada como usada, el validator
    no_repeticion debe fallar y el orchestrator regenera.
    """
    estado = EstadoCapturado()
    frases_usadas = ["Aquí trabajamos muy de la mano con las familias"]
    respuesta_repetida = (
        "Entiendo tu preocupación. Aquí trabajamos muy de la mano con las familias, "
        "y por eso el bullying es un tema que abordamos directo."
    )
    report = run_all_validators(
        respuesta=respuesta_repetida,
        estado=estado,
        frases_usadas=frases_usadas,
    )
    assert report.all_passed is False
    failed = report.failed_map
    assert "no_repeticion" in failed
    # El feedback explica al modelo qué corregir
    feedback = report.feedback_para_regenerar()
    assert feedback is not None
    assert "ya la usaste" in feedback or "no_repeticion" in feedback


def test_bug_pregunta_escuela_actual_ya_conocida() -> None:
    """Producción 13-may: papá dijo 'sí, está en otra escuela en 1° de primaria'.
    2 minutos después Sofía preguntó '¿Está en alguna escuela ahorita?'.
    """
    estado = EstadoCapturado(
        hijos=[
            HijoInfo(
                nombre="Mateo",
                edad=8,
                nivel=NivelEducativo.PRIMARIA,
                escuela_actual="otra escuela",
            )
        ]
    )
    respuesta_repite = "Qué bueno saberlo. Cuéntame, ¿está ahorita en alguna escuela?"
    report = run_all_validators(
        respuesta=respuesta_repite,
        estado=estado,
        intent=Intent.PREGUNTA_GENERAL_MAPLE,
    )
    assert "no_pregunta_repetida" in report.failed_map


def test_bug_nivel_buscado_ya_conocido_no_se_repregunta() -> None:
    """Si el estado dice nivel_buscado_actual=primaria y la respuesta vuelve a preguntar
    el nivel, falla.
    """
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.PRIMARIA)
    respuesta = "Antes de continuar, ¿en qué nivel está tu hijo?"
    report = run_all_validators(respuesta=respuesta, estado=estado)
    assert "no_pregunta_repetida" in report.failed_map


def test_bug_envio_fantasma_de_imagen() -> None:
    """Sofía dijo 'ya te envié la tabla de costos' sin llamar a send_image."""
    estado = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.KINDER,
        pidio_costos=True,
    )
    respuesta_fantasma = "Aquí tienes — ya te envié la tabla de costos con todos los conceptos."
    report = run_all_validators(
        respuesta=respuesta_fantasma,
        estado=estado,
        intent=Intent.PREGUNTA_COSTOS,
        tools_called=[],  # NO se llamó send_image
    )
    assert "no_envio_fantasma" in report.failed_map


def test_bug_envio_fantasma_pasa_si_tool_llamado() -> None:
    """Mismo escenario pero CON el tool llamado: pasa."""
    estado = EstadoCapturado()
    respuesta = "Te acabo de enviar la imagen de la tabla."
    report = run_all_validators(
        respuesta=respuesta,
        estado=estado,
        intent=Intent.PREGUNTA_COSTOS,
        tools_called=["send_image"],
    )
    assert "no_envio_fantasma" not in report.failed_map


def test_bug_evasion_pregunta_costos_sin_numero() -> None:
    """Papá pregunta costos, Sofía evade hablando de filosofía. Falla."""
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.PRIMARIA)
    respuesta_evasiva = (
        "Más que el precio, lo importante es entender lo que reciben tus hijos. "
        "Maple es una propuesta educativa única."
    )
    report = run_all_validators(
        respuesta=respuesta_evasiva,
        estado=estado,
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert "no_evasion" in report.failed_map


def test_bug_evasion_costos_aclarando_nivel_pasa() -> None:
    """Si la respuesta pregunta el nivel antes de cotizar (legítimo), pasa."""
    estado = EstadoCapturado()
    respuesta = "Con gusto. ¿Para qué nivel buscas el costo?"
    report = run_all_validators(
        respuesta=respuesta,
        estado=estado,
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert "no_evasion" not in report.failed_map


def test_bug_evasion_costos_con_dejame_confirmar_pasa() -> None:
    """Si Sofía dice 'déjame confirmar con el equipo' (caso donde no sabe), pasa."""
    estado = EstadoCapturado()
    respuesta = "Es buena pregunta. Déjame confirmar el dato con el equipo y te respondo."
    report = run_all_validators(
        respuesta=respuesta,
        estado=estado,
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert "no_evasion" not in report.failed_map


def test_bug_extraer_frases_municion_para_tracking() -> None:
    """Tras una respuesta con frase munición, el orchestrator debe trackearla
    para que validators futuros la detecten como repetida.
    """
    respuesta = (
        "Aquí trabajamos muy de la mano con las familias porque "
        "el desarrollo no pasa solo en el salón."
    )
    detectadas = extraer_frases_municion_usadas(respuesta)
    # Detecta al menos las dos variantes de la siembra de alianza
    assert any("trabajamos muy de la mano" in f for f in detectadas)
    assert any("desarrollo no pasa solo" in f for f in detectadas)


def test_bug_multiples_validators_fallan_juntos() -> None:
    """Una respuesta MUY mala puede romper varios validators a la vez.
    Verificamos que report.failed los recolecta todos para regeneración informada.
    """
    estado = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.PRIMARIA,
        hijos=[HijoInfo(edad=8, escuela_actual="otra")],
    )
    respuesta_mala = (
        "Aquí trabajamos muy de la mano con las familias. "
        "Ya te envié la tabla. ¿Está ahorita en alguna escuela? "
        "¿En qué nivel está tu hijo?"
    )
    report = run_all_validators(
        respuesta=respuesta_mala,
        estado=estado,
        intent=Intent.PREGUNTA_NIVEL,
        tools_called=[],
        frases_usadas=["Aquí trabajamos muy de la mano con las familias"],
    )
    failed = set(report.failed_map.keys())
    assert "no_repeticion" in failed
    assert "no_envio_fantasma" in failed
    assert "no_pregunta_repetida" in failed
    # Feedback agrupa todos los problemas
    feedback = report.feedback_para_regenerar()
    assert feedback is not None
    assert feedback.count("- ") >= 3  # al menos 3 bullets
