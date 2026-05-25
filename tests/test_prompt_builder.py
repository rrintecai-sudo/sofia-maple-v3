"""Tests del prompt_builder."""

from __future__ import annotations

import pytest
from app.core.prompt_builder import (
    build_system_blocks,
    clear_cache,
    estimate_total_tokens,
    load_prompt_file,
)
from app.core.state import (
    EstadoCapturado,
    EstadoConversacion,
    FaseJourney,
    HijoInfo,
    Modo,
    NivelEducativo,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Limpia el cache de archivos entre tests."""
    clear_cache()
    yield
    clear_cache()


def test_load_identity_strips_frontmatter() -> None:
    text = load_prompt_file("identity.md")
    assert not text.startswith("---")
    assert "IDENTIDAD" in text
    assert "Sofía" in text


def test_load_rules() -> None:
    text = load_prompt_file("rules.md")
    assert "REGLAS DURAS" in text
    assert "tutea" in text.lower()


def test_load_vocabulario() -> None:
    text = load_prompt_file("vocabulario.md")
    assert "VOCABULARIO" in text
    assert "FRASES MAPLE DE ALTO IMPACTO" in text


def test_load_all_journey_files_exist() -> None:
    for fase in FaseJourney:
        text = load_prompt_file(f"journey/{fase.value}.md")
        assert len(text) > 100, f"journey/{fase.value}.md está vacío"


def test_load_modo_aprendizaje() -> None:
    text = load_prompt_file("modo_aprendizaje.md")
    assert "MODO APRENDIZAJE" in text


def test_load_nonexistent_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt_file("inexistente.md")


def test_build_blocks_bienvenida() -> None:
    """Estado inicial → identity + rules + vocabulario + bienvenida + dynamic."""
    estado = EstadoConversacion.nueva("web:test1")
    blocks = build_system_blocks(estado)
    # 3 always + 1 journey + 1 dynamic = 5 bloques
    assert len(blocks) == 5
    assert all(b["type"] == "text" for b in blocks)
    # primeros 4 son cacheables (default enable_prompt_caching=True)
    for b in blocks[:4]:
        assert b.get("cache_control") == {"type": "ephemeral"}
    # El último NO cacheable
    assert "cache_control" not in blocks[-1]


def test_build_blocks_includes_journey_for_phase() -> None:
    """Fase descubrimiento → carga journey/descubrimiento.md."""
    estado = EstadoConversacion.nueva("web:test2")
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    blocks = build_system_blocks(estado)
    journey_text = blocks[3]["text"]
    assert "DESCUBRIMIENTO" in journey_text
    assert "ALIANZA ESCUELA-FAMILIA" in journey_text


def test_build_blocks_agendado_includes_post_agendado() -> None:
    """Si agendado=True, también carga post_agendado.md (anti-insistencia)."""
    estado = EstadoConversacion.nueva("web:test3")
    estado.fase_journey = FaseJourney.INFORMACION
    estado.agendado = True
    blocks = build_system_blocks(estado)
    full_text = "\n".join(b["text"] for b in blocks)
    assert "Anti-insistencia post-agendado" in full_text


def test_build_blocks_modo_aprendizaje() -> None:
    """Modo aprendizaje → carga el archivo extra."""
    estado = EstadoConversacion.nueva("web:test4")
    estado.modo = Modo.APRENDIZAJE
    blocks = build_system_blocks(estado)
    full_text = "\n".join(b["text"] for b in blocks)
    assert "MODO APRENDIZAJE" in full_text


def test_build_blocks_includes_captured_state() -> None:
    """Datos capturados aparecen en el bloque dinámico final."""
    estado = EstadoConversacion.nueva("whatsapp:5218441302112@s.whatsapp.net")
    estado.estado_capturado = EstadoCapturado(
        nombre_papa="Juan",
        nivel_buscado_actual=NivelEducativo.PRIMARIA,
        hijos=[HijoInfo(nombre="Mateo", edad=8, escuela_actual="otra escuela")],
        miedos=["bullying"],
    )
    blocks = build_system_blocks(estado)
    dynamic = blocks[-1]["text"]
    assert "Juan" in dynamic
    assert "Mateo" in dynamic
    assert "primaria" in dynamic
    assert "bullying" in dynamic
    assert "otra escuela" in dynamic


def test_build_blocks_no_captured_state_no_block() -> None:
    """Sin estado capturado, no aparece esa sección (solo meta)."""
    estado = EstadoConversacion.nueva("web:empty")
    blocks = build_system_blocks(estado)
    dynamic = blocks[-1]["text"]
    assert "CONTEXTO DEL TURNO" in dynamic
    assert "ESTADO YA CAPTURADO" not in dynamic


def test_build_blocks_meta_includes_canal_fase_modo() -> None:
    estado = EstadoConversacion.nueva("telegram:99999")
    estado.fase_journey = FaseJourney.OBJECIONES
    blocks = build_system_blocks(estado)
    dynamic = blocks[-1]["text"]
    assert "telegram" in dynamic
    assert "objeciones" in dynamic
    assert "normal" in dynamic


def test_frases_usadas_aparece_en_dynamic() -> None:
    estado = EstadoConversacion.nueva("web:frases")
    estado.frases_usadas = ["Aquí trabajamos muy de la mano con las familias"]
    blocks = build_system_blocks(estado)
    dynamic = blocks[-1]["text"]
    assert "no las repitas" in dynamic
    assert "trabajamos muy de la mano" in dynamic


def test_estimate_total_tokens_reasonable() -> None:
    """Total de tokens del prompt debería estar ~5000-8000 (vs ~40000 antiguo)."""
    estado = EstadoConversacion.nueva("web:size")
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    blocks = build_system_blocks(estado)
    total = estimate_total_tokens(blocks)
    # Banda amplia para no ser flaky pero detectar regresión grande
    assert 3000 < total < 12000, f"Token estimate fuera de banda: {total}"


def test_caching_can_be_disabled(monkeypatch):
    """Si ENABLE_PROMPT_CACHING=false, los bloques no tienen cache_control."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_PROMPT_CACHING", "false")
    estado = EstadoConversacion.nueva("web:nocache")
    blocks = build_system_blocks(estado)
    for b in blocks:
        assert "cache_control" not in b
    get_settings.cache_clear()


# ============================================================
# Fix A.4 (2026-05-19, feedback Lily): estancia solo si el papá pregunta
# ============================================================


def test_informacion_prompt_tiene_regla_estancia_solo_si_pregunta() -> None:
    """El prompt de informacion debe instruir explícitamente a NO ofrecer
    estancia automáticamente cuando preguntan costos.

    Test smoke del contenido del prompt — verifica que la regla del feedback
    de Lily quedó incluida. Tests del comportamiento real requieren golden
    test con LLM (caro), por eso aquí solo se valida el texto del prompt.
    """
    estado = EstadoConversacion.nueva("web:test")
    estado.fase_journey = FaseJourney.INFORMACION
    blocks = build_system_blocks(estado)
    full_prompt = "\n\n".join(b.get("text", "") for b in blocks).lower()

    assert "no ofrezcas estancia automáticamente" in full_prompt, (
        "Falta la regla 'NO ofrezcas estancia automáticamente' en el prompt"
    )
    # Debe instruir condicionalidad por keywords
    assert "sin mencionar" in full_prompt and "estancia" in full_prompt
    # Debe prohibir la pregunta robótica
    assert "no uses la pregunta robótica" in full_prompt


def test_informacion_prompt_no_tiene_pregunta_automatica_costos_estancia() -> None:
    """Anti-regression: la regla previa 'pregunta primero ¿colegiatura o
    estancia?' como instrucción POSITIVA fue eliminada. Si aparece
    'pregunta primero' debe ser dentro del contexto de prohibición."""
    info_md = load_prompt_file("journey/informacion.md").lower()
    # La frase robótica solo puede aparecer como ejemplo de qué NO hacer
    if "pregunta primero" in info_md:
        # Si existe, debe haber una prohibición explícita cerca
        assert "no uses la pregunta" in info_md, (
            "El prompt menciona 'pregunta primero' pero no la prohíbe explícitamente"
        )


# ============================================================
# Fix B.4 (2026-05-19, reunión Maple): longitud de respuestas en descubrimiento
# ============================================================


def test_descubrimiento_prompt_tiene_regla_longitud() -> None:
    """El prompt de descubrimiento debe instruir máximo 4 párrafos en
    explicaciones de método/filosofía/etapas."""
    import re

    desc_raw = load_prompt_file("journey/descubrimiento.md").lower()
    # Normalizar removiendo asteriscos de markdown (** y *) para tests robustos
    desc_md = re.sub(r"\*+", "", desc_raw)

    assert "máximo 4 párrafos" in desc_md, (
        "Falta la regla 'máximo 4 párrafos' en descubrimiento"
    )
    # Debe prohibir bullets en respuestas sobre filosofía/valores/hijo
    assert "nunca uses bullets" in desc_md
    assert "prosa fluida" in desc_md


def test_descubrimiento_prompt_lista_excepciones_bullets() -> None:
    """Los bullets están permitidos para horarios/costos/servicios/requisitos."""
    desc_md = load_prompt_file("journey/descubrimiento.md").lower()
    assert "horarios concretos" in desc_md
    assert "costos detallados" in desc_md
