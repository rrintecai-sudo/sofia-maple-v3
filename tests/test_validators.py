"""Tests de los 5 validators determinísticos."""

from __future__ import annotations

from app.core.intent_classifier import Intent
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo
from app.core.validators import (
    FRASES_MUNICION,
    extraer_frases_municion_usadas,
    run_all_validators,
    validar_no_envio_fantasma,
    validar_no_evasion,
    validar_no_markdown_excesivo,
    validar_no_pregunta_repetida,
    validar_no_repeticion,
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


def test_run_all_returns_5_results() -> None:
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Hola, ¿cómo te puedo ayudar?",
        estado=estado,
        intent=Intent.SALUDO_INICIAL,
    )
    assert len(report.results) == 5
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
    estado = EstadoCapturado()
    report = run_all_validators(
        respuesta="Ya te envié todo.",
        estado=estado,
        tools_called=[],
    )
    passed = report.passed_map
    failed = report.failed_map
    assert isinstance(passed, dict) and len(passed) == 5
    assert "no_envio_fantasma" in failed
