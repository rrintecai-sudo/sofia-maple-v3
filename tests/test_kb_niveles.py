"""Tests de los archivos KB modulares por nivel (Fix B.5).

Verifica que los 14 archivos .md en `app/kb/niveles/` existen, son parseables
con frontmatter YAML válido, y contienen las frases canónicas del PDF oficial
de Cecilia (En blanco 26.pdf).

Tests funcionales (comportamiento de Sofía con LLM) viven en golden tests —
estos son smoke tests del contenido del KB para detectar regresiones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.ingest_niveles_kb import parse_frontmatter

KB_DIR = Path(__file__).resolve().parent.parent / "app" / "kb" / "niveles"

NIVELES_ESPERADOS = {
    "maternal_cubs.md": ("cubs", "maternal", 3, 11),
    "maternal_babies.md": ("babies", "maternal", 12, 18),
    "maternal_infants.md": ("infants", "maternal", 18, 24),
    "maternal_toddlers.md": ("toddlers", "maternal", 24, 36),
    "kinder_1.md": ("kinder_1", "kinder", 36, 48),
    "kinder_2.md": ("kinder_2", "kinder", 48, 60),
    "kinder_3.md": ("kinder_3", "kinder", 60, 72),
    "primaria_1.md": ("primaria_1", "primaria", 72, 84),
    "primaria_2.md": ("primaria_2", "primaria", 84, 96),
    "primaria_3.md": ("primaria_3", "primaria", 96, 108),
    "secundaria_1.md": ("secundaria_1", "secundaria", 144, 156),
    "secundaria_2.md": ("secundaria_2", "secundaria", 156, 168),
    "secundaria_3.md": ("secundaria_3", "secundaria", 168, 180),
    "transversales.md": ("transversales", "transversal", 0, 999),
}


def test_los_14_archivos_existen() -> None:
    for filename in NIVELES_ESPERADOS:
        path = KB_DIR / filename
        assert path.exists(), f"Falta {filename} en {KB_DIR}"


@pytest.mark.parametrize("filename,expected", list(NIVELES_ESPERADOS.items()))
def test_frontmatter_de_cada_archivo(filename: str, expected: tuple[str, str, int, int]) -> None:
    """Cada archivo tiene frontmatter con nivel/categoria/edad correctas."""
    nivel_esp, categoria_esp, edad_min, edad_max = expected
    text = (KB_DIR / filename).read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)

    assert metadata.get("nivel") == nivel_esp, (
        f"{filename}: nivel esperado={nivel_esp} got={metadata.get('nivel')}"
    )
    assert metadata.get("categoria") == categoria_esp
    assert metadata.get("edad_min_meses") == edad_min
    assert metadata.get("edad_max_meses") == edad_max
    assert body, f"{filename}: body vacío"
    # Source debe apuntar al PDF de Cecilia
    assert "En blanco 26.pdf" in (metadata.get("source") or ""), (
        f"{filename}: falta atribución al PDF de Cecilia"
    )


# ============================================================
# Frases canónicas del PDF — verificación de contenido textual
# ============================================================


def test_cubs_tiene_los_4_pilares_del_pdf() -> None:
    """Cubs PDF: 'el vínculo, la seguridad, la exploración, el lenguaje'."""
    text = (KB_DIR / "maternal_cubs.md").read_text(encoding="utf-8").lower()
    for pilar in ["el vínculo", "la seguridad", "la exploración", "el lenguaje"]:
        assert pilar in text, f"Cubs falta pilar '{pilar}'"
    # Frase emblemática del PDF
    assert "no buscamos trabajar lo académico" in text


def test_kinder_archivos_marcan_nota_critica_no_pbl() -> None:
    """Los 3 archivos de Kinder deben tener nota_critica con prohibición PBL."""
    for nivel in ["kinder_1.md", "kinder_2.md", "kinder_3.md"]:
        text = (KB_DIR / nivel).read_text(encoding="utf-8").lower()
        assert "nunca mencionar" in text and "pbl" in text, f"{nivel} no tiene nota_critica con PBL"


def test_kinder_archivos_no_mencionan_pbl_en_copy() -> None:
    """El COPY del cuerpo (no la nota_critica del frontmatter) NO debe
    mencionar PBL ni 'proyectos' como metodología."""
    for nivel in ["kinder_1.md", "kinder_2.md", "kinder_3.md"]:
        text = (KB_DIR / nivel).read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        body_low = body.lower()
        assert "pbl" not in body_low, f"{nivel}: cuerpo menciona PBL"
        assert "challenge based" not in body_low, f"{nivel}: cuerpo menciona Challenge Based"
        # Pero SÍ debe mencionar aprendizaje activo / juego intencional
        # (al menos en kinder_1 y kinder_2; kinder_3 puede usar otros términos)


def test_kinder_1_tiene_frase_aprendizaje_activo() -> None:
    """1° Kinder: 'aprendizaje activo' + 'juego intencional' del PDF."""
    text = (KB_DIR / "kinder_1.md").read_text(encoding="utf-8").lower()
    assert "aprendizaje" in text and "activa" in text
    assert "juego intencional" in text


def test_primaria_1_si_menciona_aprendizaje_activo() -> None:
    """Primaria 1 SÍ habla de aprendizaje activo (transición desde Kinder)."""
    text = (KB_DIR / "primaria_1.md").read_text(encoding="utf-8").lower()
    assert "aprendizaje activo" in text


def test_secundaria_1_si_menciona_proyectos() -> None:
    """Secundaria 1 PDF: 'proyectos, debate, investigación, análisis'."""
    text = (KB_DIR / "secundaria_1.md").read_text(encoding="utf-8").lower()
    assert "proyectos" in text
    assert "debate" in text
    assert "investigación" in text


def test_transversales_tiene_bullying_y_idiomas() -> None:
    """Transversales debe cubrir bullying, presión académica e idiomas."""
    text = (KB_DIR / "transversales.md").read_text(encoding="utf-8").lower()
    # Frase canónica anti-bullying del PDF
    assert "el miedo forme carácter" in text or "el miedo no forma carácter" in text
    # Presión académica
    assert "presión académica" in text
    # Idiomas (inglés y francés del PDF)
    assert "inglés" in text
    assert "francés" in text


def test_transversales_no_dice_bullying_casi_no_existe() -> None:
    """Anti-regression: en la reunión 19-may, Sofía decía 'el bullying casi
    no existe'. El PDF oficial NO usa esa frase."""
    text = (KB_DIR / "transversales.md").read_text(encoding="utf-8").lower()
    assert "casi no existe" not in text
