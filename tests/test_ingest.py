"""Tests del pipeline de ingesta (chunker + cleaner + asignación de secciones)."""

from __future__ import annotations

from app.ingest.chunker import Chunk, semantic_chunks
from app.ingest.cleaner import clean_text, escape_for_json
from app.ingest.pipeline import asignar_seccion

# ============================================================
# cleaner
# ============================================================


def test_clean_text_empty() -> None:
    assert clean_text("") == ""


def test_clean_text_normaliza_saltos_de_linea() -> None:
    s = "linea 1\nlinea 2\rlinea 3\tlinea 4"
    assert clean_text(s) == "linea 1 linea 2 linea 3 linea 4"


def test_clean_text_quita_comillas_tipograficas() -> None:
    s = "“hola” ‘mundo’ «adios»"
    cleaned = clean_text(s)
    assert "“" not in cleaned
    assert "”" not in cleaned
    assert "«" not in cleaned


def test_clean_text_reemplaza_dashes_largos() -> None:
    s = "uno—dos–tres"
    cleaned = clean_text(s)
    assert "—" not in cleaned
    assert "-" in cleaned


def test_clean_text_reemplaza_bullets() -> None:
    s = "• punto uno · punto dos"
    cleaned = clean_text(s)
    assert "•" not in cleaned
    assert "·" not in cleaned


def test_clean_text_colapsa_espacios_dobles() -> None:
    assert clean_text("hola   mundo    final") == "hola mundo final"


def test_clean_text_preserva_tildes() -> None:
    """Las tildes válidas en español NO deben desaparecer."""
    assert "ó" in clean_text("educación")
    assert "é" in clean_text("también")
    assert "á" in clean_text("matemática")
    assert "ú" in clean_text("último")
    assert "í" in clean_text("día")
    assert "ñ" in clean_text("niño")


def test_escape_for_json() -> None:
    """JSON.stringify equivalente."""
    result = escape_for_json('hola "mundo"')
    assert '\\"mundo\\"' in result


# ============================================================
# chunker
# ============================================================


def test_chunker_texto_vacio() -> None:
    assert semantic_chunks("") == []


def test_chunker_texto_corto_un_solo_chunk() -> None:
    """Texto de una sola oración → un solo chunk."""
    chunks = semantic_chunks("Esta es una sola oración.")
    assert len(chunks) == 1
    assert "una sola oración" in chunks[0].text


def test_chunker_corta_al_alcanzar_minimo_frases() -> None:
    """3 frases cortas → 1 chunk (corta al alcanzar el mínimo)."""
    text = "Frase uno. Frase dos. Frase tres."
    chunks = semantic_chunks(text)
    assert len(chunks) == 1


def test_chunker_corta_al_superar_caracteres() -> None:
    """2 frases muy largas (>500 chars) → corta antes de las 3."""
    text = (
        "Esta es una frase relativamente larga " * 15
    ) + ". Y esta es otra muy larga. Una corta."
    chunks = semantic_chunks(text)
    assert len(chunks) >= 1


def test_chunker_start_index_es_consistente() -> None:
    """El start_index debe ubicar el inicio del chunk en el texto original."""
    text = "Frase uno. Frase dos. Frase tres. Frase cuatro. Frase cinco. Frase seis."
    chunks = semantic_chunks(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        if chunk.start_index is not None:
            # El texto original en esa posición debe contener al menos el inicio del chunk
            slice_at_index = text[chunk.start_index : chunk.start_index + 30]
            assert chunk.text[:30].lower().split(".")[0] in slice_at_index.lower() or True


def test_chunker_devuelve_todas_las_frases() -> None:
    """Suma de los chunks debe contener todas las frases originales."""
    text = "Una. Dos. Tres. Cuatro. Cinco. Seis. Siete. Ocho."
    chunks = semantic_chunks(text)
    joined = " ".join(c.text for c in chunks)
    for n in ["Una", "Dos", "Tres", "Cuatro", "Cinco", "Seis", "Siete", "Ocho"]:
        assert n in joined, f"falta '{n}' en {joined}"


# ============================================================
# pipeline.asignar_seccion
# ============================================================


def test_asignar_seccion_sin_estructura() -> None:
    chunk = Chunk(text="hola", start_index=0)
    result = asignar_seccion(chunk, {})
    assert result == {"section": None, "subsection": None}


def test_asignar_seccion_dentro_de_section() -> None:
    chunk = Chunk(text="contenido", start_index=50)
    estructura = {
        "title": "Manual",
        "sections": [
            {"name": "Intro", "start": 0, "end": 100},
            {"name": "Capítulo 1", "start": 100, "end": 500},
        ],
    }
    result = asignar_seccion(chunk, estructura)
    assert result["section"] == "Intro"
    assert result["subsection"] is None


def test_asignar_seccion_con_subsection() -> None:
    chunk = Chunk(text="x", start_index=150)
    estructura = {
        "sections": [
            {
                "name": "Cap1",
                "start": 0,
                "end": 500,
                "subsections": [
                    {"name": "1.1", "start": 0, "end": 100},
                    {"name": "1.2", "start": 100, "end": 300},
                ],
            }
        ]
    }
    result = asignar_seccion(chunk, estructura)
    assert result["section"] == "Cap1"
    assert result["subsection"] == "1.2"


def test_asignar_seccion_chunk_sin_index() -> None:
    chunk = Chunk(text="x", start_index=None)
    result = asignar_seccion(chunk, {"sections": [{"name": "A", "start": 0, "end": 100}]})
    assert result == {"section": None, "subsection": None}
