"""Tests del state_extractor."""

from __future__ import annotations

import pytest
from app.core.state import EstadoCapturado, HijoInfo, NivelEducativo
from app.core.state_extractor import (
    ExtraccionTurno,
    _aplicar_fallbacks_deterministicos,
    _es_nombre_valido,
    _es_presentacion_explicita,
    _nombre_junto_a_edad,
    _parse_extraction,
    aplicar_extraccion,
    extraer_edad_simple,
    extraer_email,
    extraer_grado_simple,
    extraer_nombre_hijo,
    extraer_nombre_papa,
    extraer_telefono,
    nombre_hijo_por_contexto,
    nombre_papa_por_contexto,
    parsear_bundle_papa_hijo,
)

# ============================================================
# FIX (2026-06-04) — parser acotado del bundle "X, hijo Y" (caso María)
# ============================================================


@pytest.mark.parametrize(
    "mensaje,papa,hijo",
    [
        # Caso REAL de María: papá + hijo en un turno.
        ("maria urdaneta, hijo juan david wilchez", "Maria Urdaneta", "Juan David Wilchez"),
        ("Ana López, mi hija se llama Lucía", "Ana López", "Lucía"),
        ("soy Pedro, mi hijo es Diego", "Pedro", "Diego"),
        # Solo hijo (sin papá antes del marcador) → papá None.
        ("se llama Emanuel", None, "Emanuel"),
        ("mi hijo Mateo", None, "Mateo"),
        # Sin marcador de hijo → no aplica el bundle.
        ("Oscar Rodriguez", None, None),
        ("hola quiero agendar", None, None),
    ],
)
def test_parsear_bundle_papa_hijo(mensaje, papa, hijo) -> None:
    assert parsear_bundle_papa_hijo(mensaje) == (papa, hijo)


def test_fallback_bundle_captura_papa_y_hijo() -> None:
    """El bundle de María: 'maria urdaneta, hijo juan david wilchez' → AMBOS slots,
    el papá NO se cae por la regla de no-explícito."""
    res = _aplicar_fallbacks_deterministicos(
        ExtraccionTurno(), "maria urdaneta, hijo juan david wilchez"
    )
    assert res.nombre_papa == "Maria Urdaneta"
    assert res.nombre_papa_explicito is True  # sobrevive el drop de no-explícito
    assert res.nombre_hijo == "Juan David Wilchez"


@pytest.mark.parametrize(
    "declarado,esperado",
    [
        ("primero de primaria", "1° de Primaria"),
        ("segundo de primaria", "2° de Primaria"),
        ("tercero de kinder", "3° de Kinder"),
        ("2do primaria", "2° de Primaria"),
        ("1° de Primaria", "1° de Primaria"),  # ya canónico, idempotente
    ],
)
def test_fallback_canonicaliza_grado_declarado(declarado, esperado) -> None:
    """Cambio A: un grado declarado en palabra se canonicaliza ('primero de
    primaria' → '1° de Primaria') para que la edad NO lo pise (Política A)."""
    res = _aplicar_fallbacks_deterministicos(ExtraccionTurno(grado_hijo=declarado), declarado)
    assert res.grado_hijo == esperado


# ============================================================
# FIX (2026-06-02) — capa de captura DETERMINÍSTICA consolidada
# ============================================================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("mi correo es oscar@example.com", "oscar@example.com"),
        ("ing2oscar@gmail.com, +17866035862", "ing2oscar@gmail.com"),
        ("ana.perez@correo.mx y mi cel", "ana.perez@correo.mx"),
        ("sin correo aquí", None),
    ],
)
def test_extraer_email(texto, esperado) -> None:
    assert extraer_email(texto) == esperado


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("+17866035862", "+17866035862"),
        ("mi cel 8441234567", "8441234567"),
        ("844 123 4567", "8441234567"),
        ("844-123-45-67", "8441234567"),
        ("+52 844 123 4567", "+528441234567"),
        ("tengo 4 años", None),  # no es teléfono
    ],
)
def test_extraer_telefono(texto, esperado) -> None:
    assert extraer_telefono(texto) == esperado


def test_extraer_telefono_no_confunde_digitos_del_email() -> None:
    # El email tiene dígitos; al excluirlo, el único teléfono es el celular.
    msg = "ing2oscar@gmail.com, +17866035862"
    email = extraer_email(msg)
    assert extraer_telefono(msg, excluir=email) == "+17866035862"


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("tiene 4 años", 4),
        ("Mateo, 5 añitos", 5),
        ("de 6 años", 6),
        ("tengo 4 hijos", None),  # NO es edad (no dice 'años')
        ("kinder 2", None),
    ],
)
def test_extraer_edad_simple(texto, esperado) -> None:
    assert extraer_edad_simple(texto) == esperado


def test_fallback_edad_numero_suelto_con_contexto() -> None:
    """Cuando el gate pidió la EDAD, un número suelto ('5', 'tiene 5') es la edad."""
    for msg in ("5", "tiene 5", "5 añitos"):
        res = _aplicar_fallbacks_deterministicos(ExtraccionTurno(), msg, ultimo_campo_pedido="edad")
        assert res.edad_hijo == 5, msg
    # Sin contexto de edad, un '5' suelto NO se vuelve edad.
    res = _aplicar_fallbacks_deterministicos(
        ExtraccionTurno(), "5", ultimo_campo_pedido="nombre_papa"
    )
    assert res.edad_hijo is None


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("yo soy Pedro Rojas, ing2oscar@gmail.com", "Pedro Rojas"),
        ("me llamo Ana", "Ana"),
        ("mi nombre es Juan Carlos Pérez", "Juan Carlos Pérez"),
        ("soy la mamá de Lucía", None),  # 'la' corta → no es nombre
        ("hola, quiero info", None),
    ],
)
def test_extraer_nombre_papa(texto, esperado) -> None:
    assert extraer_nombre_papa(texto) == esperado


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("se llama Emanuel", "Emanuel"),
        ("mi hijo Emanuel", "Emanuel"),
        ("mi hija se llama Lucía", "Lucía"),
        ("el niño se llama Diego", "Diego"),
        ("mi hijo tiene 4 años", None),  # 'tiene' NO es nombre
        ("mi pequeño", None),  # no hay nombre real
        ("hola quiero info", None),
    ],
)
def test_extraer_nombre_hijo(texto, esperado) -> None:
    assert extraer_nombre_hijo(texto) == esperado


def test_fallback_se_llama_no_va_a_nombre_papa() -> None:
    # 'se llama Emanuel' (sin edad) → es el NIÑO, no el papá.
    res = _aplicar_fallbacks_deterministicos(ExtraccionTurno(), "se llama Emanuel")
    assert res.nombre_hijo == "Emanuel"
    assert res.nombre_papa is None


@pytest.mark.parametrize(
    "ultimo,mensaje,esperado",
    [
        ("¿Y tu nombre, por favor?", "Oscar Rodriguez", "Oscar Rodriguez"),
        ("¿Cómo te llamas?", "Ana", "Ana"),
        ("pregunta ÚNICAMENTE por: tu nombre.", "soy Pedro Rojas", "Pedro Rojas"),
        # Sofía NO pidió el nombre → no captura nombre suelto.
        ("¿Qué día te queda mejor?", "Oscar Rodriguez", None),
        # Trae correo/teléfono → no es un nombre limpio.
        ("¿tu nombre?", "ing2oscar@gmail.com, 7866035862", None),
        # Confirmación / palabras-función → no es nombre.
        ("¿tu nombre?", "si ya te lo dije", None),
        ("¿tu nombre?", "no gracias", None),
        # Nombre + correo + teléfono JUNTOS → extrae solo el nombre.
        (
            "¿Me compartes tu nombre, correo y celular?",
            "Oscar Rodriguez, ing2oscar@gmail.com, +17866035862",
            "Oscar Rodriguez",
        ),
        # Presentación del HIJO con apellido tras "¿tu nombre?" → NO es el papá.
        ("¿y tu nombre?", "se llama Emanuel Rodriguez", None),
    ],
)
def test_nombre_papa_por_contexto(ultimo, mensaje, esperado) -> None:
    assert nombre_papa_por_contexto(mensaje, ultimo) == esperado


@pytest.mark.parametrize(
    "ultimo,mensaje,esperado",
    [
        ("¿Me confirmas el nombre completo de tu hijo?", "Emanuel Rodriguez", "Emanuel Rodriguez"),
        ("¿cómo se llama tu peque?", "Emanuel", "Emanuel"),
        ("pregunta ÚNICAMENTE por: el nombre completo del niño/a.", "Ana López", "Ana López"),
        # Sofía pidió el nombre del PAPÁ → no captura como hijo.
        ("¿y tu nombre?", "Emanuel Rodriguez", None),
        # Sofía pidió el día → no captura nombre.
        ("¿Qué día te queda mejor?", "Emanuel Rodriguez", None),
    ],
)
def test_nombre_hijo_por_contexto(ultimo, mensaje, esperado) -> None:
    assert nombre_hijo_por_contexto(mensaje, ultimo) == esperado


def test_fallback_nombre_hijo_suelto_tras_pregunta() -> None:
    # El bug nuevo: 'Emanuel Rodriguez' SUELTO tras "¿nombre de tu hijo?".
    res = _aplicar_fallbacks_deterministicos(
        ExtraccionTurno(),
        "Emanuel Rodriguez",
        ultimo_assistant="¿Me confirmas el nombre completo de tu hijo?",
    )
    assert res.nombre_hijo == "Emanuel Rodriguez"
    assert res.nombre_papa is None  # NO sangra al papá


def test_fallback_captura_nombre_papa_suelto_tras_pregunta() -> None:
    # El bug de Emanuel: 'Oscar Rodriguez' como respuesta a "¿tu nombre?".
    res = _aplicar_fallbacks_deterministicos(
        ExtraccionTurno(), "Oscar Rodriguez", ultimo_assistant="Listo. ¿Y tu nombre, por favor?"
    )
    assert res.nombre_papa == "Oscar Rodriguez"
    assert res.nombre_papa_explicito is True
    assert res.nombre_hijo is None


@pytest.mark.parametrize(
    "nombre,valido",
    [
        ("Mateo", True),
        ("Pedro Rojas", True),
        ("pequeño", False),
        ("bebé", False),
        ("niño", False),
        ("tiene", False),
        ("es", False),
        ("", False),
        (None, False),
    ],
)
def test_es_nombre_valido(nombre, valido) -> None:
    assert _es_nombre_valido(nombre) is valido


def test_fallback_descarta_nombre_hijo_invento() -> None:
    # El LLM devolvió 'pequeño' como nombre del hijo → se descarta (no se inventa).
    res = _aplicar_fallbacks_deterministicos(
        ExtraccionTurno(nombre_hijo="pequeño", edad_hijo=4), "mi pequeño tiene 4 años"
    )
    assert res.nombre_hijo is None
    assert res.edad_hijo == 4  # la edad sí se conserva


def test_fallback_captura_todo_en_un_mensaje_corrido() -> None:
    # Mensaje real de la prueba de Pedro: TODO en un mensaje, SIN LLM.
    msg = (
        "hola quiero agendar mi hijo Mateo tiene 4 años yo soy Pedro Rojas, "
        "ing2oscar@gmail.com, +17866035862 el viernes 10am"
    )
    res = _aplicar_fallbacks_deterministicos(ExtraccionTurno(), msg)
    assert res.nombre_papa == "Pedro Rojas"
    assert res.nombre_papa_explicito is True
    assert res.nombre_hijo == "Mateo"
    assert res.edad_hijo == 4
    assert res.email_papa == "ing2oscar@gmail.com"
    assert res.telefono == "+17866035862"


# ============================================================
# FIX (e) 2026-06-01 — "yo soy Oscar" corrige nombre_papa clavado
# ============================================================


@pytest.mark.parametrize(
    "texto",
    ["yo soy Oscar Rodriguez", "Emanuel, yo soy Oscar", "me llamo Ana", "mi nombre es Luis"],
)
def test_es_presentacion_explicita_positivos(texto) -> None:
    assert _es_presentacion_explicita(texto) is True


def test_presentacion_explicita_marca_flag() -> None:
    extr = ExtraccionTurno(nombre_papa="Oscar Rodriguez")
    fixed = _aplicar_fallbacks_deterministicos(extr, "Emanuel Rodriguez, yo soy Oscar Rodriguez")
    assert fixed.nombre_papa_explicito is True


def test_aplicar_extraccion_explicito_sobreescribe_clavado() -> None:
    """'Jose' clavado de sesión contaminada; 'yo soy Oscar' lo corrige."""
    actual = EstadoCapturado(nombre_papa="Jose")
    extr = ExtraccionTurno(nombre_papa="Oscar Rodriguez", nombre_papa_explicito=True)
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nombre_papa == "Oscar Rodriguez"


def test_aplicar_extraccion_no_explicito_no_sobreescribe() -> None:
    """Sin presentación explícita NO se pisa un nombre ya capturado."""
    actual = EstadoCapturado(nombre_papa="Jose")
    extr = ExtraccionTurno(nombre_papa="Oscar", nombre_papa_explicito=False)
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nombre_papa == "Jose"


# ============================================================
# FIX (c) 2026-06-01 — "Jose, 4 años" → Jose es el NIÑO
# ============================================================


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Jose, 4 años", "Jose"),
        ("Jose, 4 anos", "Jose"),
        ("Jose de 4 años", "Jose"),
        ("Ana, 3 añitos", "Ana"),
        ("Diego, 5 años", "Diego"),
    ],
)
def test_nombre_junto_a_edad_positivos(texto, esperado) -> None:
    assert _nombre_junto_a_edad(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["tengo un hijo de 4 años", "mi hijo tiene 4 años", "tiene 5 años", "quiero kinder", "hola"],
)
def test_nombre_junto_a_edad_negativos(texto) -> None:
    assert _nombre_junto_a_edad(texto) is None


def test_fallback_corrige_nombre_papa_a_hijo() -> None:
    """El LLM metió 'Jose' como nombre del papá; (c) lo mueve al hijo."""
    buggy = ExtraccionTurno(nombre_papa="Jose", edad_hijo=4, nivel_buscado="kinder")
    fixed = _aplicar_fallbacks_deterministicos(buggy, "Jose, 4 años")
    assert fixed.nombre_hijo == "Jose"
    assert fixed.nombre_papa is None


def test_fallback_conserva_nombre_papa_explicito() -> None:
    """'yo soy Oscar Rodriguez' (presentación explícita) → se conserva el nombre."""
    extr = ExtraccionTurno(nombre_papa="Oscar Rodriguez")
    fixed = _aplicar_fallbacks_deterministicos(extr, "yo soy Oscar Rodriguez, oscar@x.com")
    assert fixed.nombre_papa == "Oscar Rodriguez"
    assert fixed.nombre_papa_explicito is True
    assert fixed.nombre_hijo is None


def test_fallback_descarta_nombre_papa_no_explicito() -> None:
    """FIX (2026-06-02): un nombre_papa del LLM SIN señal explícita se descarta
    (evita que el nombre/apellido del hijo sangre al slot del papá)."""
    extr = ExtraccionTurno(nombre_papa="Emanuel Rodriguez")
    fixed = _aplicar_fallbacks_deterministicos(extr, "se llama Emanuel Rodriguez")
    assert fixed.nombre_papa is None  # ← NO se acepta como papá
    assert fixed.nombre_hijo == "Emanuel Rodriguez"  # ← es el hijo (nombre+apellido)


def test_aplicar_extraccion_libera_nombre_papa_mal_asignado() -> None:
    """Si 'Jose' quedó clavado como papá y luego se sabe que es el hijo,
    se libera el slot para que el papá real (Oscar) entre después."""
    # turno previo dejó nombre_papa="Jose" (mal)
    actual = EstadoCapturado(nombre_papa="Jose")
    # turno actual: el extractor ya corrigió → nombre_hijo="Jose"
    extr = ExtraccionTurno(nombre_hijo="Jose")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nombre_papa is None  # liberado
    assert nuevo.hijos and nuevo.hijos[0].nombre == "Jose"

    # ahora sí puede entrar Oscar
    nuevo2 = aplicar_extraccion(nuevo, ExtraccionTurno(nombre_papa="Oscar Rodriguez"))
    assert nuevo2.nombre_papa == "Oscar Rodriguez"


# ============================================================
# extraer_grado_simple (FIX 2026-06-01 — "2 kinder" → "2° de Kinder")
# ============================================================


@pytest.mark.parametrize(
    "texto,grado,nivel",
    [
        ("2 kinder", "2° de Kinder", "kinder"),
        ("2do kinder", "2° de Kinder", "kinder"),
        ("kinder 3", "3° de Kinder", "kinder"),
        ("segundo de kinder", "2° de Kinder", "kinder"),
        ("va en kinder 2", "2° de Kinder", "kinder"),
        ("3ro de primaria", "3° de Primaria", "primaria"),
        ("5to primaria", "5° de Primaria", "primaria"),
        ("primaria 6", "6° de Primaria", "primaria"),
        ("1 secundaria", "1° de Secundaria", "secundaria"),
    ],
)
def test_extraer_grado_simple_positivos(texto, grado, nivel) -> None:
    g, n = extraer_grado_simple(texto)
    assert g == grado
    assert n == nivel


@pytest.mark.parametrize(
    "texto", ["tengo 4 años", "somos 3 hijos", "hola", "quiero info", "kinder", ""]
)
def test_extraer_grado_simple_no_falsos_positivos(texto) -> None:
    assert extraer_grado_simple(texto) == (None, None)


def test_parse_extraction_valid_json() -> None:
    raw = '{"nivel_buscado": "primaria", "pidio_costos": true}'
    result = _parse_extraction(raw)
    assert result.nivel_buscado == "primaria"
    assert result.pidio_costos is True


def test_parse_extraction_with_backticks() -> None:
    raw = '```json\n{"nivel_buscado": "kinder"}\n```'
    result = _parse_extraction(raw)
    assert result.nivel_buscado == "kinder"


def test_parse_extraction_invalid_json_returns_empty() -> None:
    result = _parse_extraction("no es json")
    assert result.nivel_buscado is None
    assert result.pidio_costos is False


def test_aplicar_extraccion_nivel_buscado() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nivel_buscado="primaria")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nivel_buscado_actual == NivelEducativo.PRIMARIA
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].nivel == NivelEducativo.PRIMARIA


def test_aplicar_extraccion_no_sobrescribe_nombre() -> None:
    actual = EstadoCapturado(nombre_papa="Juan")
    extr = ExtraccionTurno(nombre_papa="Pedro")
    nuevo = aplicar_extraccion(actual, extr)
    # nombre original se mantiene (no sobrescribe)
    assert nuevo.nombre_papa == "Juan"


def test_aplicar_extraccion_pidio_costos_sticky() -> None:
    actual = EstadoCapturado(pidio_costos=True)
    extr = ExtraccionTurno(pidio_costos=False)
    nuevo = aplicar_extraccion(actual, extr)
    # True no se reescribe a False
    assert nuevo.pidio_costos is True


def test_aplicar_extraccion_miedos_acumula_sin_dedup() -> None:
    actual = EstadoCapturado(miedos=["bullying"])
    extr = ExtraccionTurno(miedos_nuevos=["bullying", "que no aprenda"])
    nuevo = aplicar_extraccion(actual, extr)
    assert "bullying" in nuevo.miedos
    assert "que no aprenda" in nuevo.miedos
    assert nuevo.miedos.count("bullying") == 1  # no duplica


def test_aplicar_extraccion_upsert_hijo_existente() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nombre="Mateo", nivel=NivelEducativo.PRIMARIA)])
    extr = ExtraccionTurno(
        nivel_buscado="primaria",
        edad_hijo=8,
        escuela_actual="otra escuela",
    )
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].edad == 8
    assert nuevo.hijos[0].escuela_actual == "otra escuela"
    assert nuevo.hijos[0].nombre == "Mateo"  # mantiene


def test_aplicar_extraccion_crea_nuevo_hijo_si_nivel_distinto() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nombre="Mateo", nivel=NivelEducativo.PRIMARIA)])
    extr = ExtraccionTurno(nivel_buscado="kinder", nombre_hijo="Sofía")
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 2
    nombres = {h.nombre for h in nuevo.hijos}
    assert nombres == {"Mateo", "Sofía"}


def test_aplicar_extraccion_nivel_invalido_ignora() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nivel_buscado="universidad")  # no existe
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nivel_buscado_actual is None
    assert len(nuevo.hijos) == 0


def test_aplicar_extraccion_diagnostico_no_sobrescribe() -> None:
    actual = EstadoCapturado(hijos=[HijoInfo(nivel=NivelEducativo.PRIMARIA, diagnostico="autismo")])
    extr = ExtraccionTurno(nivel_buscado="primaria", diagnostico_hijo="otro")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.hijos[0].diagnostico == "autismo"


# ============================================================
# Fix B.1 (2026-05-19, reunión Maple): cantidad_hijos vs edad_hijo
#
# Bug: el extractor LLM confundía "tengo 4 hijos" con "tiene 4 años".
# Tests usan _parse_extraction con JSON simulado — testean el schema
# y que `cantidad_hijos` sea campo separado de `edad_hijo`.
#
# La calidad del prompt LLM (few-shot) se valida en golden tests
# con conversación real cuando se redeploye.
# ============================================================


def test_extraccion_acepta_cantidad_hijos_separado() -> None:
    """Schema permite cantidad_hijos sin tocar edad_hijo (bug B.1)."""
    raw = '{"cantidad_hijos": 4, "edad_hijo": null}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos == 4
    assert result.edad_hijo is None


def test_extraccion_acepta_edad_hijo_sin_cantidad() -> None:
    """'Mi hijo tiene 4 años' → edad_hijo=4, cantidad_hijos=null."""
    raw = '{"cantidad_hijos": null, "edad_hijo": 4}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos is None
    assert result.edad_hijo == 4


def test_extraccion_ambiguo_ambos_null() -> None:
    """Mensaje ambiguo '4' sin contexto → ambos null (Sofía pregunta)."""
    raw = '{"cantidad_hijos": null, "edad_hijo": null}'
    result = _parse_extraction(raw)
    assert result.cantidad_hijos is None
    assert result.edad_hijo is None


def test_extraccion_cantidad_hijos_validacion_rango() -> None:
    """cantidad_hijos debe estar en 0-10. Valor fuera de rango → fallback."""
    raw = '{"cantidad_hijos": 50}'
    result = _parse_extraction(raw)
    # Pydantic rechaza → fallback a ExtraccionTurno() vacío
    assert result.cantidad_hijos is None


def test_aplicar_no_pone_cantidad_hijos_como_edad() -> None:
    """`cantidad_hijos` NO se copia a edad del hijo — fix del bug raíz.

    Si LLM extrae solo cantidad_hijos=4 (papá dijo 'tengo 4 hijos'),
    el estado NO debe terminar con edad=4 en ningún hijo, y NO debe
    crearse un HijoInfo solo por la cantidad.
    """
    actual = EstadoCapturado()
    extr = ExtraccionTurno(cantidad_hijos=4)
    nuevo = aplicar_extraccion(actual, extr)
    # NO se crea hijo solo por cantidad (no hay otro dato de hijo)
    assert len(nuevo.hijos) == 0
    # Y obviamente ninguno tiene edad=4
    assert all(h.edad != 4 for h in nuevo.hijos)


def test_aplicar_edad_hijo_correcto_si_es_edad() -> None:
    """Si el LLM mete edad_hijo=4 (papá dijo 'tiene 4 años'), sí se aplica."""
    actual = EstadoCapturado()
    extr = ExtraccionTurno(edad_hijo=4)
    nuevo = aplicar_extraccion(actual, extr)
    assert len(nuevo.hijos) == 1
    assert nuevo.hijos[0].edad == 4


# ============================================================
# Fix C.1.A — extractor debe capturar nombre_papa (faltaba regla + few-shot)
# Bug detectado en prod 2026-05-25: papá dijo "Me llamo Oscar Rodriguez"
# y nombre_papa quedó None → handler de agendado no pudo crear lead
# (parent_name NOT NULL) → Sofía alucinó la confirmación.
# ============================================================


def test_system_prompt_documenta_nombre_papa() -> None:
    """El system prompt enumera explícitamente nombre_papa en sus reglas y
    contiene ejemplos few-shot."""
    from app.core.state_extractor import _SYSTEM_PROMPT

    prompt_low = _SYSTEM_PROMPT.lower()
    # Regla
    assert "nombre_papa" in _SYSTEM_PROMPT
    # Patrones canónicos
    for pat in ["me llamo", "soy ", "mi nombre es", "habla la mamá"]:
        assert pat in prompt_low, f"few-shot patrón ausente: {pat!r}"
    # Disambiguación contra nombre_hijo
    assert "nombre del hijo" in prompt_low or "nombre_hijo" in _SYSTEM_PROMPT


def test_parse_extraction_nombre_papa() -> None:
    """Plumbing: si el LLM devuelve nombre_papa, el parser lo conserva."""
    raw = '{"nombre_papa": "Oscar Rodriguez", "nivel_buscado": "kinder", "edad_hijo": 5}'
    result = _parse_extraction(raw)
    assert result.nombre_papa == "Oscar Rodriguez"
    assert result.nivel_buscado == "kinder"
    assert result.edad_hijo == 5


def test_aplicar_extraccion_nombre_papa_nuevo() -> None:
    """Si nombre_papa estaba None, se aplica el nuevo."""
    actual = EstadoCapturado()
    extr = ExtraccionTurno(nombre_papa="Oscar Rodriguez")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.nombre_papa == "Oscar Rodriguez"


# ============================================================
# D.3 (Lily 2026-05-27): email_papa y telefono
# ============================================================


def test_aplicar_extraccion_email_nuevo() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(email_papa="oscar@example.com")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.email_papa == "oscar@example.com"


def test_aplicar_extraccion_email_no_sobrescribe() -> None:
    actual = EstadoCapturado(email_papa="ana@example.com")
    extr = ExtraccionTurno(email_papa="otro@example.com")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.email_papa == "ana@example.com"


def test_aplicar_extraccion_telefono_nuevo() -> None:
    actual = EstadoCapturado()
    extr = ExtraccionTurno(telefono="8441234567")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.telefono == "8441234567"


def test_aplicar_extraccion_telefono_no_sobrescribe() -> None:
    actual = EstadoCapturado(telefono="8441234567")
    extr = ExtraccionTurno(telefono="9999999999")
    nuevo = aplicar_extraccion(actual, extr)
    assert nuevo.telefono == "8441234567"


def test_extractor_prompt_documenta_email_y_telefono() -> None:
    """El system prompt del extractor debe instruir cómo detectar email y celular."""
    from app.core.state_extractor import _SYSTEM_PROMPT

    p = _SYSTEM_PROMPT.lower()
    assert "email_papa" in p
    assert "telefono" in p
    assert "celular" in p
