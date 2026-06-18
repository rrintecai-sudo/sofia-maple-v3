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
    """Estado inicial → identity + rules + KB oficial + vocabulario + bienvenida + dynamic."""
    estado = EstadoConversacion.nueva("web:test1")
    blocks = build_system_blocks(estado)
    # 2 always + KB oficial + vocabulario + 1 journey + 1 dynamic = 6 bloques
    assert len(blocks) == 6
    assert all(b["type"] == "text" for b in blocks)
    # Anthropic permite máx 4 bloques cacheables: identity, rules, KB y journey.
    # vocabulario (idx 3) y el dynamic (último) van SIN cache.
    cacheables = [
        i for i, b in enumerate(blocks) if b.get("cache_control") == {"type": "ephemeral"}
    ]
    assert cacheables == [0, 1, 2, 4]
    # El último NO cacheable
    assert "cache_control" not in blocks[-1]


def test_build_blocks_includes_journey_for_phase() -> None:
    """Fase descubrimiento → carga journey/descubrimiento.md (robusto a la posición)."""
    estado = EstadoConversacion.nueva("web:test2")
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    blocks = build_system_blocks(estado)
    textos = [b["text"] for b in blocks]
    assert any("DESCUBRIMIENTO" in t and "ALIANZA ESCUELA-FAMILIA" in t for t in textos)


def test_build_blocks_agendado_includes_post_agendado() -> None:
    """Si agendado=True, también carga post_agendado.md (anti-insistencia)."""
    estado = EstadoConversacion.nueva("web:test3")
    estado.fase_journey = FaseJourney.INFORMACION
    estado.agendado = True
    blocks = build_system_blocks(estado)
    full_text = "\n".join(b["text"] for b in blocks)
    assert "Anti-insistencia post-agendado" in full_text


# ============================================================
# Fix C.1.B — journey/agendado.md prioriza el hint del handler sobre la
# plantilla canned "Listo, [nombre]. Te confirmo tu cita". Bug visto en
# prod 2026-05-25: Sofía decía "Te confirmo tu cita" aún sin que el
# handler hubiera creado la cita en BD.
# ============================================================


def test_agendado_prompt_prioriza_hint_del_flujo() -> None:
    """En fase agendado, el prompt debe instruir al LLM a SEGUIR el hint
    `[FLUJO AGENDADO ...]` como fuente de verdad."""
    estado = EstadoConversacion.nueva("web:fixc1b")
    estado.fase_journey = FaseJourney.AGENDADO
    blocks = build_system_blocks(estado)
    full = "\n".join(b["text"] for b in blocks)
    # Regla crítica documentada
    assert "FLUJO AGENDADO" in full
    assert "fuente de verdad" in full.lower()


def test_agendado_prompt_prohibe_te_confirmo_sin_aprobacion() -> None:
    """El prompt debe enseñar que NO se diga 'te confirmo tu cita' cuando
    la cita está pendiente de aprobación de Lily."""
    estado = EstadoConversacion.nueva("web:fixc1b2")
    estado.fase_journey = FaseJourney.AGENDADO
    blocks = build_system_blocks(estado)
    full = "\n".join(b["text"] for b in blocks)
    full_low = full.lower()
    # El prompt debe contener la prohibición explícita
    assert "nunca digas" in full_low
    assert "te confirmo tu cita" in full_low
    assert "pendiente" in full_low


# ============================================================
# Bloque C.2 — prompt prohíbe preguntar campus + manda mencionar el campus
# asignado por el sistema. Regla Lily 2026-05-24.
# ============================================================


def test_agendado_prompt_prohibe_preguntar_campus() -> None:
    """El prompt debe prohibir preguntar al papá qué campus prefiere."""
    estado = EstadoConversacion.nueva("web:c2-1")
    estado.fase_journey = FaseJourney.AGENDADO
    blocks = build_system_blocks(estado)
    full = "\n".join(b["text"] for b in blocks)
    full_low = full.lower()
    # Frases prohibidas mencionadas en el prompt
    assert "campus" in full_low
    # El sistema asigna, NO pregunta
    assert "nunca preguntes" in full_low or "se asigna" in full_low or "se resuelve" in full_low


def test_agendado_prompt_documenta_regla_lily() -> None:
    """El prompt enumera explícitamente la regla Campus 1 / Campus 2 por nivel."""
    estado = EstadoConversacion.nueva("web:c2-2")
    estado.fase_journey = FaseJourney.AGENDADO
    blocks = build_system_blocks(estado)
    full = "\n".join(b["text"] for b in blocks)
    # Campus 1 cubre Maternal/Kinder/Primaria 1-5
    assert "Campus 1" in full
    assert "Maternal" in full
    assert "Kinder" in full
    assert "Primaria 1°" in full or "Primaria 1-5" in full or "Primaria 1°-5°" in full
    # Campus 2 cubre Primaria 6° + Secundaria
    assert "Campus 2" in full
    assert "Primaria 6°" in full or "6° Primaria" in full or "Primaria 6" in full
    assert "Secundaria" in full


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
    """Total de tokens del prompt. Desde que el KB oficial (~56KB) es un bloque del
    prompt (fuente de verdad de Sofía), el total ronda ~20-24k. Banda amplia para
    detectar una regresión grande sin ser flaky."""
    estado = EstadoConversacion.nueva("web:size")
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    blocks = build_system_blocks(estado)
    total = estimate_total_tokens(blocks)
    assert 15000 < total < 32000, f"Token estimate fuera de banda: {total}"


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

    assert "máximo 4 párrafos" in desc_md, "Falta la regla 'máximo 4 párrafos' en descubrimiento"
    # Debe prohibir bullets en respuestas sobre filosofía/valores/hijo
    assert "nunca uses bullets" in desc_md
    assert "prosa fluida" in desc_md


def test_descubrimiento_prompt_lista_excepciones_bullets() -> None:
    """Los bullets están permitidos para horarios/costos/servicios/requisitos."""
    desc_md = load_prompt_file("journey/descubrimiento.md").lower()
    assert "horarios concretos" in desc_md
    assert "costos detallados" in desc_md


# ============================================================
# Fix B.6 (2026-05-19, PDF Journey Maple): micro-tensión + alianza
# ============================================================


def test_descubrimiento_prompt_tiene_micro_tension_con_ejemplos() -> None:
    """El prompt debe tener sección MICRO-TENSIÓN con al menos 2 ejemplos."""
    import re

    desc_raw = load_prompt_file("journey/descubrimiento.md").lower()
    desc_md = re.sub(r"\*+", "", desc_raw)

    assert "micro-tensión" in desc_md or "micro tensión" in desc_md, (
        "Falta sección MICRO-TENSIÓN en descubrimiento"
    )
    # Frases ejemplo del PDF Journey (al menos 2 deben aparecer)
    ejemplos = [
        "aprenden a cumplir",
        "no necesariamente a sostener",
        "cuando ya no hay maestro",
        "obedecer puede funcionar bien",
    ]
    matches = sum(1 for e in ejemplos if e in desc_md)
    assert matches >= 2, (
        f"Sección de micro-tensión debe tener ≥2 ejemplos concretos. "
        f"Matches encontrados: {matches}/{len(ejemplos)}"
    )
    # Regla: NO en el primer turno
    assert "no en el primer turno" in desc_md


def test_descubrimiento_alianza_obligatoria_antes_de_visita() -> None:
    """La siembra de alianza debe estar marcada como obligatoria ANTES
    de invitar a visita / cerrar tema modelo."""
    import re

    desc_raw = load_prompt_file("journey/descubrimiento.md").lower()
    desc_md = re.sub(r"\*+", "", desc_raw)

    # Debe haber referencia explícita a "antes de invitar" o "antes de cerrar"
    assert "antes" in desc_md and "invitar" in desc_md
    # Y debe afirmar que no es opcional
    assert "no es opcional" in desc_md or "no opcional" in desc_md


# ============================================================
# Fix B.7 (2026-05-19, PDF Journey): precio con contexto, no plano
# ============================================================


def test_informacion_prompt_tiene_frase_contexto_precio() -> None:
    """El prompt de costos debe instruir incluir una frase contextual
    que dé sentido al precio — no respuesta transaccional."""
    import re

    info_raw = load_prompt_file("journey/informacion.md").lower()
    info_md = re.sub(r"\*+", "", info_raw)

    # Debe mencionar "frase de contexto" o "más allá del número"
    assert "más allá del número" in info_md or "frase de contexto" in info_md, (
        "Falta frase contextual sobre el precio (más allá del número)"
    )
    # Debe mencionar "sostener lo que aprende" (frase canónica del PDF)
    assert "sostener lo que aprende" in info_md


def test_informacion_prompt_pregunta_continuacion_no_cierre_brusco() -> None:
    """El prompt debe instruir cerrar con pregunta de continuación, no
    push directo a cita inmediatamente después del precio."""
    import re

    info_raw = load_prompt_file("journey/informacion.md").lower()
    info_md = re.sub(r"\*+", "", info_raw)

    assert "pregunta de continuación" in info_md
    # Debe prohibir el push directo a cita post-precio
    assert "nunca cierres con un push directo a cita" in info_md or (
        "no cierres con un push" in info_md
    )


# ============================================================
# Fix B.5 (2026-05-19, PDF Cecilia En_blanco_26): regla Kinder ≠ PBL
# ============================================================


def test_rules_prompt_prohibe_pbl_en_kinder() -> None:
    """Regla crítica del PDF: en Kinder NUNCA mencionar PBL / proyectos /
    Challenge Based Learning."""
    import re

    rules_raw = load_prompt_file("rules.md").lower()
    rules_md = re.sub(r"\*+", "", rules_raw)

    # Debe haber sección "Kinder NO usa lenguaje de Primaria"
    assert "kinder no usa lenguaje" in rules_md
    # Debe prohibir explícitamente PBL / Challenge Based / proyectos
    assert "pbl" in rules_md
    assert "challenge based learning" in rules_md
    # Y debe mencionar el lenguaje correcto para Kinder
    assert "aprendizaje activo" in rules_md
    assert "juego intencional" in rules_md


def test_rules_prompt_cita_pdf_oficial_cecilia() -> None:
    """La regla debe referenciar el PDF oficial de Cecilia."""
    rules_md = load_prompt_file("rules.md").lower()
    # Debe haber referencia al PDF como fuente de autoridad
    assert "en blanco 26.pdf" in rules_md or "pdf oficial" in rules_md


# ============================================================
# D.1 (Gaby 2026-05-27): rules.md prohibe guiones largos/medios en respuestas
# ============================================================


def test_rules_prompt_prohibe_guiones_largos() -> None:
    rules_md = load_prompt_file("rules.md").lower()
    assert "guiones largos" in rules_md or "em-dash" in rules_md
    assert "guiones medios" in rules_md or "en-dash" in rules_md


# ============================================================
# D.2 (Gaby 2026-05-27): meta_block inyecta fecha actual + regla de día+fecha
# ============================================================


def test_meta_block_incluye_fecha_actual() -> None:
    """El bloque dinámico debe contener 'Hoy es {día} {DD} de {mes} de {YYYY}'."""
    estado = EstadoConversacion.nueva("web:d2_1")
    blocks = build_system_blocks(estado)
    dyn = blocks[-1]["text"].lower()
    assert "hoy es" in dyn
    # Alguno de los días o meses debe aparecer
    assert any(
        d in dyn for d in ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    )
    assert any(
        m in dyn
        for m in (
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        )
    )


def test_meta_block_explica_regla_dia_mas_fecha() -> None:
    """El meta-bloque debe explicar que día siempre va con fecha exacta."""
    estado = EstadoConversacion.nueva("web:d2_2")
    blocks = build_system_blocks(estado)
    dyn = blocks[-1]["text"].lower()
    assert "fecha exacta" in dyn
    assert "miércoles" in dyn  # ejemplo del bloque


def test_agendado_prompt_exige_dia_mas_fecha() -> None:
    """En fase agendado, el prompt explícitamente prohíbe decir solo el día."""
    estado = EstadoConversacion.nueva("web:d2_3")
    estado.fase_journey = FaseJourney.AGENDADO
    blocks = build_system_blocks(estado)
    full = "\n".join(b["text"] for b in blocks).lower()
    assert "día + fecha exacta" in full or "día+fecha" in full or "siempre juntos" in full
    assert "nunca menciones solo el día" in full or ("nunca" in full and "solo el día" in full)


def test_rules_incluye_postura_tareas_y_examenes() -> None:
    """FIX (2026-06-04): la postura de Maple sobre tareas/exámenes está en
    rules.md (siempre cargado) → presente en TODO prompt."""
    text = load_prompt_file("rules.md").lower()
    # Tareas: no se manda tarea a casa + prohibición de "Sí, tenemos tareas".
    assert "no" in text and "manda tareas a casa" in text
    assert "nunca" in text and 'abras con "sí, tenemos tareas"' in text
    # Exámenes: no memorización, entender y aplicar.
    assert "memorización" in text and "entienda y aplique" in text


def test_postura_tareas_en_build_system_blocks() -> None:
    """La postura de tareas se inyecta en el system prompt en cualquier fase."""
    from app.core.state import Canal, EstadoConversacion, FaseJourney

    estado = EstadoConversacion.nueva("web:t")
    estado.canal = Canal.WEB
    estado.fase_journey = FaseJourney.DESCUBRIMIENTO
    full = "\n".join(b["text"] for b in build_system_blocks(estado)).lower()
    assert "manda tareas a casa" in full
    assert 'abras con "sí, tenemos tareas"' in full


def test_rules_incluye_regla_dato_oficial_y_defiere_a_lili() -> None:
    """Costos/horarios/estancias: solo el dato inyectado; si no hay, defiere a Lili."""
    text = load_prompt_file("rules.md").lower()
    assert "dato oficial" in text
    assert "miss lili" in text
    assert "nunca" in text and ("inventar" in text or "inventes" in text)
