"""Tests de validators determinísticos.

5 con severity=error (bloquean regen) + 2 con severity=warning (solo señalan,
Bloque 5.7 ATAQUE 1)."""

from __future__ import annotations

from app.core.intent_classifier import Intent
from app.core.state import EstadoCapturado, FaseJourney, HijoInfo, NivelEducativo
from app.core.validators import (
    FRASES_MUNICION,
    extraer_frases_municion_usadas,
    run_all_validators,
    validar_no_bullets_en_descubrimiento,
    validar_no_confirma_cita_inexistente,
    validar_no_envio_fantasma,
    validar_no_evasion,
    validar_no_inventa_datos,
    validar_no_inventa_nombre_papa,
    validar_no_markdown_excesivo,
    validar_no_pregunta_repetida,
    validar_no_recita_info_no_pedida,
    validar_no_repeticion,
    validar_sin_guiones_largos,
)

# ============================================================
# validar_no_repeticion
# ============================================================


def test_no_repeticion_pasa_si_no_hay_frases_usadas() -> None:
    """Sin frases munición previas, cualquier respuesta pasa."""
    r = validar_no_repeticion(
        "Aquí trabajamos muy de la mano con las familias.",
        frases_usadas=[],
    )
    assert r.passed is True


def test_no_repeticion_falla_si_frase_munition_repetida() -> None:
    """Si la respuesta repite una frase que ya estaba en frases_usadas → falla."""
    frase = "Aquí trabajamos muy de la mano con las familias"
    r = validar_no_repeticion(
        respuesta=f"Como te decía, {frase}, esa es la realidad.",
        frases_usadas=[frase],
    )
    assert r.passed is False
    assert "trabajamos muy de la mano" in (r.reason or "").lower()
    assert r.suggested_fix is not None


def test_no_repeticion_case_insensitive() -> None:
    """Comparación es case-insensitive."""
    frase = "Los primeros años no se repiten"
    r = validar_no_repeticion(
        respuesta="LOS PRIMEROS AÑOS NO SE REPITEN. Sí.",
        frases_usadas=[frase.lower()],
    )
    assert r.passed is False


def test_no_repeticion_otra_frase_munition_no_usada() -> None:
    """Si la respuesta usa OTRA frase munición que NO está en usadas, pasa."""
    r = validar_no_repeticion(
        respuesta="No quitamos el juego, le damos intención.",
        frases_usadas=["Los primeros años no se repiten"],
    )
    assert r.passed is True


def test_frases_municion_contiene_alianza() -> None:
    """Verifica que la lista tenga la siembra de alianza (que falló en producción)."""
    found = any("trabajamos muy de la mano" in f for f in FRASES_MUNICION)
    assert found, "La frase 'trabajamos muy de la mano' debe estar en FRASES_MUNICION"


def test_extraer_frases_municion_usadas() -> None:
    """Detecta qué frases munición aparecen en una respuesta."""
    text = "Mira, los primeros años no se repiten, y aquí no quitamos el juego, le damos intención."
    found = extraer_frases_municion_usadas(text)
    assert "los primeros años no se repiten" in found
    assert "no quitamos el juego, le damos intención" in found
    assert len(found) >= 2


# ============================================================
# validar_no_envio_fantasma
# ============================================================


def test_no_envio_fantasma_pasa_sin_mencion() -> None:
    """Respuesta normal sin mencionar envíos → pasa."""
    r = validar_no_envio_fantasma(
        "La colegiatura de primaria es de $6,100 al mes.",
        tools_called=[],
    )
    assert r.passed is True


def test_no_envio_fantasma_falla_si_dice_ya_te_envie() -> None:
    """Si dice 'ya te envié la tabla' sin tool → falla."""
    r = validar_no_envio_fantasma(
        "Perfecto, ya te envié la tabla con los costos.",
        tools_called=[],
    )
    assert r.passed is False
    assert r.suggested_fix is not None


def test_no_envio_fantasma_falla_te_adjunto() -> None:
    r = validar_no_envio_fantasma(
        "Te adjunto la información de niveles.",
        tools_called=[],
    )
    assert r.passed is False


def test_no_envio_fantasma_pasa_si_tool_envio_llamado() -> None:
    """Si dice 'te envié' Y se llamó send_image → pasa."""
    r = validar_no_envio_fantasma(
        "Listo, te acabo de enviar la imagen de costos.",
        tools_called=["send_image"],
    )
    assert r.passed is True


def test_no_envio_fantasma_pasa_si_send_sticker() -> None:
    r = validar_no_envio_fantasma(
        "Te mandé un sticker de despedida.",
        tools_called=["send_sticker"],
    )
    assert r.passed is True


# ============================================================
# validar_no_pregunta_repetida
# ============================================================


def test_pregunta_repetida_pasa_si_no_hay_estado() -> None:
    """Estado vacío + cualquier pregunta → pasa."""
    estado = EstadoCapturado()
    r = validar_no_pregunta_repetida(
        "¿Para qué nivel estás buscando información?",
        estado,
    )
    assert r.passed is True


def test_pregunta_repetida_falla_si_pregunta_nivel_ya_conocido() -> None:
    """Si ya sabemos el nivel y la respuesta pregunta '¿qué nivel?' → falla."""
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.PRIMARIA)
    r = validar_no_pregunta_repetida(
        "¿Para qué nivel estás buscando?",
        estado,
    )
    assert r.passed is False
    assert "primaria" in (r.reason or "").lower()


def test_pregunta_repetida_falla_variante_etapa() -> None:
    """Variante: '¿en qué etapa está?' también cuenta como pregunta de nivel."""
    estado = EstadoCapturado(
        hijos=[HijoInfo(nivel=NivelEducativo.KINDER)],
    )
    r = validar_no_pregunta_repetida(
        "Cuéntame, ¿en qué etapa está tu hijo ahorita?",
        estado,
    )
    assert r.passed is False


def test_pregunta_repetida_falla_si_pregunta_edad_conocida() -> None:
    estado = EstadoCapturado(
        hijos=[HijoInfo(nombre="Mateo", edad=8)],
    )
    r = validar_no_pregunta_repetida(
        "¿Cuántos años tiene tu hijo?",
        estado,
    )
    assert r.passed is False


def test_pregunta_repetida_falla_si_pregunta_escuela_conocida() -> None:
    """El caso real de producción del 13-may."""
    estado = EstadoCapturado(
        hijos=[HijoInfo(escuela_actual="otra escuela")],
    )
    r = validar_no_pregunta_repetida(
        "¿Está ahorita en alguna escuela?",
        estado,
    )
    assert r.passed is False


def test_pregunta_repetida_pasa_si_pregunta_algo_distinto() -> None:
    """Pregunta por algo NO conocido → pasa."""
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.PRIMARIA)
    r = validar_no_pregunta_repetida(
        "¿Qué es lo que más te importa que pase con tu hijo?",
        estado,
    )
    assert r.passed is True


# ============================================================
# validar_no_evasion
# ============================================================


def test_evasion_pasa_si_intent_no_aplica() -> None:
    r = validar_no_evasion("respuesta cualquiera", intent=Intent.SALUDO_INICIAL)
    assert r.passed is True


def test_evasion_falla_costos_sin_numero() -> None:
    """Pregunta costos + respuesta sin número ni 'déjame confirmar' → falla."""
    r = validar_no_evasion(
        "Nuestra propuesta es muy especial, vale la pena conocerla.",
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert r.passed is False


def test_evasion_pasa_costos_con_numero() -> None:
    r = validar_no_evasion(
        "La colegiatura es de $6,100 al mes.",
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert r.passed is True


def test_evasion_pasa_costos_con_dejame_confirmar() -> None:
    r = validar_no_evasion(
        "Es una excelente pregunta. Déjame confirmar ese dato con el equipo.",
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert r.passed is True


def test_evasion_pasa_costos_aclarando_nivel() -> None:
    """Es válido pedir el nivel antes de dar el costo."""
    r = validar_no_evasion(
        "Con gusto te paso el costo. ¿Para qué nivel estás buscando?",
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert r.passed is True


def test_evasion_falla_horario_sin_hora() -> None:
    r = validar_no_evasion(
        "Nuestros horarios están diseñados para acompañar el desarrollo.",
        intent=Intent.PREGUNTA_HORARIO,
    )
    assert r.passed is False


def test_evasion_pasa_horario_con_hora() -> None:
    r = validar_no_evasion(
        "El horario de primaria es de 8:00 a 2:30.",
        intent=Intent.PREGUNTA_HORARIO,
    )
    assert r.passed is True


# ============================================================
# run_all_validators + ValidationReport
# ============================================================


def test_run_all_returns_10_results_sin_fase() -> None:
    """Sin fase_journey, run_all_validators corre 10 validators.

    8 error (incluye no_inventa_nombre_papa [ADR-020] y
    no_confirma_cita_inexistente [ADR-021]) + 2 warning."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Hola, ¿cómo te puedo ayudar?",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
    )
    assert len(report.results) == 10
    assert report.all_passed is True


def test_run_all_returns_11_results_con_fase() -> None:
    """Con fase_journey, agrega el validator de bullets-descubrimiento."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Hola, ¿cómo te puedo ayudar?",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert len(report.results) == 11
    assert report.all_passed is True


# ============================================================
# validar_no_markdown_excesivo (Bloque 5.5)
# ============================================================


def test_markdown_pasa_respuesta_natural() -> None:
    r = validar_no_markdown_excesivo(
        "¡Hola! Qué gusto saludarte. Cuéntame, ¿en qué etapa está tu hijo?"
    )
    assert r.passed is True


def test_markdown_falla_con_headers() -> None:
    r = validar_no_markdown_excesivo("# Costos\nLa colegiatura es de $6,100")
    assert r.passed is False
    assert "header" in (r.reason or "").lower()


def test_markdown_falla_con_muchas_negritas() -> None:
    txt = "**Primaria** es **especial**, con **PBL** y **CBL** y **disciplina positiva**"
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is False
    assert "negrita" in (r.reason or "").lower()


def test_markdown_pasa_con_pocas_negritas() -> None:
    r = validar_no_markdown_excesivo(
        "La colegiatura es de **$6,100 al mes** y son **11 colegiaturas**."
    )
    assert r.passed is True  # 2 negritas, OK


def test_markdown_falla_con_lista_densa() -> None:
    txt = "Los niveles son:\n- Maternal\n- Kinder\n- Primaria baja\n- Primaria alta\n- Secundaria\n"
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is False
    assert "lista" in (r.reason or "").lower()


def test_markdown_pasa_con_lista_corta() -> None:
    txt = "Tenemos:\n- Maternal\n- Kinder\n- Primaria\n"
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is True  # 3 bullets, OK


def test_markdown_falla_con_lista_numerada_larga() -> None:
    txt = (
        "Pasos:\n"
        "1. Pagar inscripción\n"
        "2. Llenar ficha\n"
        "3. Entregar documentos\n"
        "4. Entrevista\n"
        "5. Kid visit\n"
    )
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is False
    assert "numerada" in (r.reason or "").lower()


def test_markdown_pasa_con_lista_numerada_corta() -> None:
    txt = "Dos opciones:\n1. Maternal\n2. Kinder"
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is True


def test_markdown_emoji_bullets_no_son_lista() -> None:
    """✅, 📌, 🔹 NO son `-` o `*` — son emoji bullets que SÍ son OK en chat."""
    txt = "✅ Maternal\n✅ Kinder\n✅ Primaria\n✅ Secundaria\n✅ Algo más"
    r = validar_no_markdown_excesivo(txt)
    assert r.passed is True


def test_run_all_detecta_multiples_fallas() -> None:
    """Una respuesta puede fallar varios validators a la vez."""
    estado = EstadoCapturado(
        nivel_buscado_actual=NivelEducativo.PRIMARIA,
        hijos=[HijoInfo(edad=8, escuela_actual="otra")],
    )
    respuesta = (
        "Aquí trabajamos muy de la mano con las familias. "
        "Ya te envié la tabla. ¿En qué etapa está tu hijo?"
    )
    report = run_all_validators(
        respuesta=respuesta,
        estado=estado,
        intent=Intent.PREGUNTA_NIVEL,
        tools_called=[],
        frases_usadas=["Aquí trabajamos muy de la mano con las familias"],
    )
    assert report.all_passed is False
    failed_names = [r.validator for r in report.failed]
    assert "no_repeticion" in failed_names
    assert "no_envio_fantasma" in failed_names
    assert "no_pregunta_repetida" in failed_names


def test_validation_report_feedback_para_regenerar() -> None:
    """El feedback consolida los suggested_fix para inyectar al prompt."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Te adjunto la imagen.",
        estado=estado,
        intent=None,
        tools_called=[],
    )
    feedback = report.feedback_para_regenerar()
    assert feedback is not None
    assert "no_envio_fantasma" in feedback
    assert "DEBES corregir" in feedback


def test_validation_report_feedback_none_si_todo_pasa() -> None:
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Hola, qué gusto.",
        estado=estado,
    )
    assert report.feedback_para_regenerar() is None


def test_validation_report_maps_for_db() -> None:
    """passed_map/failed_map solo cuentan severity='error' (los warnings no se
    persisten en DB — Bloque 5.7 ADR-018)."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Ya te envié todo.",
        estado=estado,
        tools_called=[],
    )
    passed = report.passed_map
    failed = report.failed_map
    # 8 validators de severity=error (incluye no_inventa_nombre_papa [ADR-020] y
    # no_confirma_cita_inexistente [ADR-021]).
    assert isinstance(passed, dict) and len(passed) == 8  # solo errors
    assert "no_envio_fantasma" in failed


# ============================================================
# validar_no_inventa_datos (Bloque 5.7 ATAQUE 1 — severity=warning)
# ============================================================


def test_inventa_pasa_respuesta_neutra() -> None:
    estado = EstadoCapturado()
    r = validar_no_inventa_datos(
        "¡Hola! Cuéntame, ¿qué nivel buscas?", estado, mensajes_papa=["hola"]
    )
    assert r.passed is True
    assert r.severity == "warning"


def test_inventa_falla_vio_link_es_warning() -> None:
    """Falla pero severity=warning (no bloquea regen)."""
    r = validar_no_inventa_datos(
        "Vi el link de Instagram que me compartiste.",
        EstadoCapturado(),
        mensajes_papa=["https://instagram.com/abc"],
    )
    assert r.passed is False
    assert r.severity == "warning"
    assert "contenido externo" in (r.reason or "").lower()


def test_inventa_datos_ya_no_chequea_nombre() -> None:
    """FIX 4 (ADR-020): el sub-check de nombre se movió a
    `validar_no_inventa_nombre_papa` (error). `no_inventa_datos` ya NO lo
    evalúa, así que un nombre inventado NO lo hace fallar a él."""
    r = validar_no_inventa_datos(
        "Hola Mateo, qué gusto.",
        EstadoCapturado(),
        mensajes_papa=["hola"],
    )
    assert r.passed is True
    assert r.severity == "warning"


def test_inventa_falla_nivel_no_dicho() -> None:
    r = validar_no_inventa_datos(
        "Para tu hijo en Maternal es ideal.",
        EstadoCapturado(),
        mensajes_papa=["hola"],
    )
    assert r.passed is False


def test_inventa_pasa_nivel_en_estado() -> None:
    estado = EstadoCapturado(nivel_buscado_actual=NivelEducativo.MATERNAL)
    r = validar_no_inventa_datos(
        "Para tu hijo en Maternal, te platico.",
        estado,
        mensajes_papa=["busco maternal"],
    )
    assert r.passed is True


def test_inventa_falla_edad_no_dicha() -> None:
    r = validar_no_inventa_datos(
        "Tu hijo de 5 años está en una etapa hermosa.",
        EstadoCapturado(),
        mensajes_papa=["hola"],
    )
    assert r.passed is False


def test_inventa_pasa_genero_en_saludo_vacio() -> None:
    """Saludo inicial puro (estado y mensajes_papa vacíos) tolera 'tu hijo'."""
    r = validar_no_inventa_datos(
        "¿Qué edad tiene tu hijo?",
        EstadoCapturado(),
        mensajes_papa=[],
    )
    assert r.passed is True


def test_inventa_falla_cita_falsa() -> None:
    r = validar_no_inventa_datos(
        "Tu cita es el viernes a las 10am.",
        EstadoCapturado(cita_agendada=False),
        mensajes_papa=[],
    )
    assert r.passed is False
    assert "cita" in (r.reason or "").lower()


def test_inventa_pasa_cita_propuesta() -> None:
    r = validar_no_inventa_datos(
        "¿Te gustaría agendar una visita esta semana?",
        EstadoCapturado(cita_agendada=False),
        mensajes_papa=[],
    )
    assert r.passed is True


# ============================================================
# validar_no_bullets_en_descubrimiento (Bloque 5.7 ATAQUE 1 — severity=warning)
# ============================================================


def test_bullets_descubrimiento_pasa_otra_fase() -> None:
    r = validar_no_bullets_en_descubrimiento(
        "- a\n- b\n- c\n- d\n- e",
        fase_journey=FaseJourney.INFORMACION,
    )
    assert r.passed is True
    assert r.severity == "warning"


def test_bullets_descubrimiento_pasa_con_prosa() -> None:
    r = validar_no_bullets_en_descubrimiento(
        "Qué bonito que me cuentes eso. En Maple acompañamos con cuidado.",
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert r.passed is True


def test_bullets_descubrimiento_pasa_con_2_bullets() -> None:
    """Threshold calibrado: ≥3 bullets falla; 2 pasa."""
    r = validar_no_bullets_en_descubrimiento(
        "Aquí hacemos:\n- vínculo\n- exploración",
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert r.passed is True


def test_bullets_descubrimiento_falla_con_3_bullets() -> None:
    r = validar_no_bullets_en_descubrimiento(
        "Aquí hacemos:\n- vínculo\n- exploración\n- lenguaje",
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert r.passed is False
    assert r.severity == "warning"


def test_bullets_descubrimiento_falla_con_3_numerados() -> None:
    r = validar_no_bullets_en_descubrimiento(
        "Te recomiendo:\n1. agendar\n2. visitar\n3. preguntar",
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert r.passed is False


def test_bullets_descubrimiento_falla_con_4_negritas() -> None:
    r = validar_no_bullets_en_descubrimiento(
        "**Vínculo**, **exploración**, **lenguaje**, **autonomía**.",
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert r.passed is False


# ============================================================
# Severity: warnings NO disparan regeneración
# ============================================================


def test_warnings_no_bloquean_all_passed() -> None:
    """Si solo hay warnings y ningún error, all_passed=True (no regen)."""
    estado = EstadoCapturado()
    # Construir una respuesta que falle no_inventa_datos (warning) pero no errors
    report = run_all_validators(
        respuesta="Vi el link que me compartiste.",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
        mensajes_papa=["https://x.com"],
        fase_journey=FaseJourney.DESCUBRIMIENTO,
    )
    assert report.all_passed is True  # no errors → no regen
    assert "no_inventa_datos" in report.warnings_map


def test_warnings_map_no_incluye_errors() -> None:
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Ya te envié todo.",  # falla no_envio_fantasma (error)
        estado=estado,
        tools_called=[],
        fase_journey=FaseJourney.INFORMACION,
    )
    assert "no_envio_fantasma" in report.failed_map
    assert "no_envio_fantasma" not in report.warnings_map


def test_feedback_para_regenerar_ignora_warnings() -> None:
    """Si solo hay warnings, feedback es None (no regen)."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Vi tu link que compartiste.",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
        mensajes_papa=["url"],
    )
    assert report.feedback_para_regenerar() is None


# ============================================================
# validar_no_recita_info_no_pedida (Bloque 5.7 ATAQUE 2 — severity=warning)
# ============================================================


def test_recita_pasa_si_intent_no_aplica() -> None:
    r = validar_no_recita_info_no_pedida(
        "Recital largo " * 30,
        intent=Intent.PREGUNTA_COSTOS,
    )
    assert r.passed is True


def test_recita_pasa_respuesta_breve() -> None:
    r = validar_no_recita_info_no_pedida(
        "Perfecto, entonces hablamos de Kinder. ¿Qué te interesa saber primero?",
        intent=Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
    )
    assert r.passed is True


def test_recita_falla_respuesta_larga() -> None:
    """Tras 'Sí' del papá, Sofía recita >80 palabras → warning."""
    larga = "palabra " * 100  # 100 palabras
    r = validar_no_recita_info_no_pedida(larga, intent=Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO)
    assert r.passed is False
    assert r.severity == "warning"


def test_recita_falla_con_headers() -> None:
    r = validar_no_recita_info_no_pedida(
        "# Costos\nLa colegiatura es $6,100",
        intent=Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
    )
    assert r.passed is False
    assert "header" in (r.reason or "").lower()


def test_recita_falla_con_numerada() -> None:
    r = validar_no_recita_info_no_pedida(
        "Te recomiendo:\n1. Agendar\n2. Visitar",
        intent=Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO,
    )
    assert r.passed is False


# ============================================================
# validar_sin_guiones_largos (D.1 — Gaby 2026-05-27)
# ============================================================


def test_sin_guiones_largos_pasa_respuesta_normal() -> None:
    r = validar_sin_guiones_largos(
        "Maple no es escuela tradicional. Es educación activa para el siglo XXI."
    )
    assert r.passed is True


def test_sin_guiones_largos_pasa_con_bullet_hyphen() -> None:
    """Los bullets con '-' al inicio de línea son válidos (datos estructurados)."""
    r = validar_sin_guiones_largos("Costos:\n- Maternal: $X\n- Kinder: $Y")
    assert r.passed is True


def test_sin_guiones_largos_falla_con_em_dash() -> None:
    r = validar_sin_guiones_largos("Maple no es escuela tradicional — es educación activa.")
    assert r.passed is False
    assert "guión largo" in (r.reason or "").lower()
    assert r.suggested_fix is not None
    assert r.severity == "error"


def test_sin_guiones_largos_falla_con_en_dash() -> None:
    r = validar_sin_guiones_largos(
        "Lo notas en casa – deja de pedir respuestas y empieza a pensar."
    )
    assert r.passed is False
    assert "guión medio" in (r.reason or "").lower()


def test_sin_guiones_largos_falla_aunque_haya_uno_solo() -> None:
    """Un solo em-dash basta para fallar."""
    r = validar_sin_guiones_largos("Texto largo conversacional sin nada raro — bueno casi.")
    assert r.passed is False


def test_run_all_validators_incluye_guiones() -> None:
    """El runner global ejecuta el validator de guiones."""
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Maple es buena — sí.",
        estado=estado,
        intent=None,
    )
    nombres = [r.validator for r in report.results]
    assert "sin_guiones_largos" in nombres
    failed = [r for r in report.results if not r.passed and r.validator == "sin_guiones_largos"]
    assert len(failed) == 1


# ============================================================
# validar_no_inventa_nombre_papa (FIX 4 — ADR-020 — severity=error)
# ============================================================


def test_nombre_papa_falla_nombre_inventado() -> None:
    """Bug 'María': el papá nunca dio su nombre y Sofía lo usa → error."""
    r = validar_no_inventa_nombre_papa(
        "Hola María, con gusto te ayudo.",
        EstadoCapturado(),
        mensajes_papa=["buenas, busco info de kinder"],
    )
    assert r.passed is False
    assert r.severity == "error"
    assert "María" in (r.reason or "")


def test_nombre_papa_pasa_si_esta_en_estado() -> None:
    r = validar_no_inventa_nombre_papa(
        "Hola Oscar, con gusto te ayudo.",
        EstadoCapturado(nombre_papa="Oscar"),
        mensajes_papa=["soy oscar"],
    )
    assert r.passed is True


def test_nombre_papa_pasa_si_el_papa_lo_dijo() -> None:
    r = validar_no_inventa_nombre_papa(
        "Hola Ana, gracias por escribir.",
        EstadoCapturado(),
        mensajes_papa=["hola, soy Ana"],
    )
    assert r.passed is True


def test_nombre_papa_pasa_saludo_sin_nombre() -> None:
    r = validar_no_inventa_nombre_papa(
        "¡Hola! Con gusto te ayudo. ¿Qué nivel buscas?",
        EstadoCapturado(),
        mensajes_papa=["hola"],
    )
    assert r.passed is True


def test_nombre_papa_no_falla_por_marca_maple() -> None:
    """Calibración anti-falso-positivo: 'Claro, Maple ofrece...' NO es un
    nombre del papá."""
    r = validar_no_inventa_nombre_papa(
        "Claro, Maple ofrece el mejor método activo. Perfecto, seguimos.",
        EstadoCapturado(),
        mensajes_papa=["cuéntame del método"],
    )
    assert r.passed is True


def test_nombre_papa_no_falla_por_gracias_por() -> None:
    r = validar_no_inventa_nombre_papa(
        "Gracias por tu mensaje. Oye, ¿qué edad tiene tu peque?",
        EstadoCapturado(),
        mensajes_papa=["hola"],
    )
    assert r.passed is True


# ============================================================
# validar_no_confirma_cita_inexistente (FIX 2/3 — ADR-021 — severity=error)
# ============================================================


def test_confirma_cita_falla_registre_solicitud_sin_cita() -> None:
    """Frase exacta del bug real: confirma sin appointment_id → error."""
    r = validar_no_confirma_cita_inexistente(
        "Registré tu solicitud, en breve Lily te confirma y te comparte la dirección.",
        cita_realmente_registrada=False,
    )
    assert r.passed is False
    assert r.severity == "error"


def test_confirma_cita_falla_te_agendo_para() -> None:
    r = validar_no_confirma_cita_inexistente(
        "Listo, te agendo para mañana viernes 30 de mayo a las 9 a.m. en Campus 1.",
        cita_realmente_registrada=False,
    )
    assert r.passed is False


def test_confirma_cita_pasa_si_cita_existe() -> None:
    """Si hay cita real, confirmar es correcto → NO bloquea."""
    r = validar_no_confirma_cita_inexistente(
        "Registré tu solicitud, en breve Lily te confirma.",
        cita_realmente_registrada=True,
    )
    assert r.passed is True


def test_confirma_cita_pasa_mensaje_de_proceso_condicional() -> None:
    """Calibración: mensaje legítimo de proceso ('cuando me confirmes los
    datos, registro tu solicitud') NO se bloquea."""
    r = validar_no_confirma_cita_inexistente(
        "En cuanto me confirmes tu nombre y correo, registro tu solicitud de cita.",
        cita_realmente_registrada=False,
    )
    assert r.passed is True


def test_confirma_cita_pasa_invitacion() -> None:
    r = validar_no_confirma_cita_inexistente(
        "¿Te gustaría que agendemos una visita esta semana? ¿Qué día te queda bien?",
        cita_realmente_registrada=False,
    )
    assert r.passed is True


def test_confirma_cita_pasa_pide_datos() -> None:
    r = validar_no_confirma_cita_inexistente(
        "Para dejar todo listo, ¿me compartes tu nombre, correo y celular?",
        cita_realmente_registrada=False,
    )
    assert r.passed is True


def test_run_all_validators_incluye_nuevos_error_validators() -> None:
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Hola, ¿qué nivel buscas?",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
        cita_realmente_registrada=False,
    )
    nombres = [r.validator for r in report.results]
    assert "no_inventa_nombre_papa" in nombres
    assert "no_confirma_cita_inexistente" in nombres
