"""Test E2E del wiring del handler de agendado en el orchestrator (Bloque C.1 PASO 8).

Verifica que cuando intent==QUIERE_AGENDAR, `procesar_turno()`:
- Llama a handle_appointment_intent
- Inyecta el hint del handler al user message del LLM
- NO rompe el turno si el handler falla

Mockea pesadamente las dependencias externas (LLM, Supabase, repository).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.core.appointment_flow import AppointmentHandlerResult
from app.core.intent_classifier import Intent, IntentResult
from app.core.orchestrator import procesar_turno
from app.core.state import Canal, EstadoConversacion


class _FakeMessage:
    """Anthropic Message mock con content como lista de bloques."""

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Usage:
        input_tokens = 100
        output_tokens = 50
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    def __init__(self, text: str) -> None:
        self.content = [self._Block(text)]
        self.usage = self._Usage()


@pytest.fixture
def estado_con_nombre():
    return EstadoConversacion.nueva("telegram:111")


@pytest.fixture
def repo_mock(estado_con_nombre):
    """Mock del repository — devuelve estado fresco, persiste no-op."""
    repo = AsyncMock()
    repo.get_conversation = AsyncMock(return_value=estado_con_nombre)
    repo.upsert_conversation = AsyncMock()
    repo.list_recent_messages = AsyncMock(return_value=[])
    repo.insert_message = AsyncMock()
    repo.insert_turn_log = AsyncMock()
    repo.count_turns = AsyncMock(return_value=1)
    return repo


@pytest.mark.asyncio
async def test_orchestrator_llama_handler_cuando_intent_es_agendar(repo_mock, monkeypatch) -> None:
    """Si el classifier devuelve QUIERE_AGENDAR, el orchestrator llama al
    handler y el hint llega al LLM."""

    # Capturamos los `messages` que recibe Anthropic para verificar el hint
    captured_user_messages: list[str] = []

    async def fake_anthropic_chat(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        for m in msgs:
            if m.get("role") == "user":
                captured_user_messages.append(m["content"])
        return _FakeMessage("Perfecto, registré tu solicitud. En breve te confirmamos.")

    fake_anthropic = AsyncMock()
    fake_anthropic.chat = AsyncMock(side_effect=fake_anthropic_chat)

    # El handler devuelve un hint claramente identificable
    handler_result = AppointmentHandlerResult(
        hint_para_prompt="[FLUJO AGENDADO TEST HINT INYECTADO]",
        acciones=["appointment_created", "event_emitted"],
        lead_id=42,
        appointment_id=99,
    )
    fake_handler = AsyncMock(return_value=handler_result)

    # Intent classifier devuelve QUIERE_AGENDAR
    fake_classify = AsyncMock(
        return_value=IntentResult(
            intent=Intent.QUIERE_AGENDAR, confidence=0.95, razonamiento_breve="agendar"
        )
    )
    # Extractor de estado devuelve datos vacíos
    from app.core.state_extractor import ExtraccionTurno

    fake_extract = AsyncMock(return_value=ExtraccionTurno())

    with (
        patch("app.core.orchestrator.get_repository", return_value=repo_mock),
        patch("app.core.orchestrator.get_anthropic", return_value=fake_anthropic),
        patch("app.core.orchestrator.handle_appointment_intent", fake_handler),
        patch("app.core.orchestrator.classify_intent", fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", fake_extract),
        # Evitar que llame a tools.campus / niveles
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
    ):
        result = await procesar_turno(
            mensaje="quiero agendar para el martes 10am",
            session_id="telegram:111",
            canal=Canal.TELEGRAM,
        )

    # El handler fue llamado
    fake_handler.assert_called_once()
    # El hint del handler llegó al mensaje al LLM
    assert captured_user_messages
    assert any("TEST HINT INYECTADO" in m for m in captured_user_messages)
    # La respuesta de Sofía se devuelve normal
    assert "registré tu solicitud" in result.response


@pytest.mark.asyncio
async def test_orchestrator_no_llama_handler_si_intent_no_es_agendar(
    repo_mock,
) -> None:
    """Si el intent es otro (ej. PREGUNTA_COSTOS), handle_appointment_intent
    NO se llama."""
    fake_anthropic = AsyncMock()
    fake_anthropic.chat = AsyncMock(return_value=_FakeMessage("Cuesta X."))

    fake_handler = AsyncMock(
        return_value=AppointmentHandlerResult(hint_para_prompt="X", acciones=[])
    )

    fake_classify = AsyncMock(
        return_value=IntentResult(
            intent=Intent.PREGUNTA_COSTOS, confidence=0.9, razonamiento_breve="x"
        )
    )

    from app.core.state_extractor import ExtraccionTurno

    fake_extract = AsyncMock(return_value=ExtraccionTurno())

    with (
        patch("app.core.orchestrator.get_repository", return_value=repo_mock),
        patch("app.core.orchestrator.get_anthropic", return_value=fake_anthropic),
        patch("app.core.orchestrator.handle_appointment_intent", fake_handler),
        patch("app.core.orchestrator.classify_intent", fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
    ):
        await procesar_turno(
            mensaje="cuánto cuesta?",
            session_id="telegram:111",
            canal=Canal.TELEGRAM,
        )

    fake_handler.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_resiliente_si_handler_falla(repo_mock) -> None:
    """Si el handler levanta excepción, el orchestrator NO se rompe — la
    conversación continúa con respuesta normal del LLM."""
    fake_anthropic = AsyncMock()
    fake_anthropic.chat = AsyncMock(return_value=_FakeMessage("respuesta normal"))

    # Handler que explota
    fake_handler = AsyncMock(side_effect=RuntimeError("BOOM"))

    fake_classify = AsyncMock(
        return_value=IntentResult(
            intent=Intent.QUIERE_AGENDAR, confidence=0.95, razonamiento_breve="x"
        )
    )

    from app.core.state_extractor import ExtraccionTurno

    fake_extract = AsyncMock(return_value=ExtraccionTurno())

    with (
        patch("app.core.orchestrator.get_repository", return_value=repo_mock),
        patch("app.core.orchestrator.get_anthropic", return_value=fake_anthropic),
        patch("app.core.orchestrator.handle_appointment_intent", fake_handler),
        patch("app.core.orchestrator.classify_intent", fake_classify),
        patch("app.core.orchestrator.extraer_de_mensaje", fake_extract),
        patch("app.core.orchestrator.get_campus_para_nivel", AsyncMock(return_value=None)),
        patch("app.core.orchestrator.consultar_edades_de_nivel", AsyncMock(return_value=None)),
    ):
        result = await procesar_turno(
            mensaje="quiero agendar",
            session_id="telegram:111",
            canal=Canal.TELEGRAM,
        )

    # El turno completó a pesar de que el handler explotó
    assert result.response == "respuesta normal"
    fake_handler.assert_called_once()
