"""Guards de texto libre de Haiku: frases prohibidas + tope de preguntas."""

from __future__ import annotations

import pytest
from app.core.output_guards import (
    limitar_preguntas,
    sanear_frases_prohibidas,
    sanear_texto_libre_haiku,
)


@pytest.mark.parametrize(
    "entrada,prohibida",
    [
        ("¡Qué bueno! ¿está en escuela? ¿Cómo lo viven?", "cómo lo viven"),
        ("Me encanta. ¿Cuál te atrae? ¿Y qué días te vienen mejor?", "te vienen mejor"),
        ("El precio está regalado para lo que ofrecemos.", "regalado"),
        ("Súper chévere la escuela.", "chévere"),
    ],
)
def test_sanear_frases_prohibidas_quita_la_oracion(entrada, prohibida) -> None:
    out = sanear_frases_prohibidas(entrada)
    assert prohibida.lower() not in out.lower()


def test_frases_no_prohibidas_quedan_intactas() -> None:
    txt = "Perfecto, 2° de Kinder. Te queda bien la escuela."
    assert sanear_frases_prohibidas(txt) == txt


def test_limitar_preguntas_conserva_la_primera() -> None:
    txt = "Qué bien. ¿Vives cerca? ¿Buscas kinder? ¿Cuándo vienes?"
    out = limitar_preguntas(txt, maximo=1)
    assert out.count("?") == 1
    assert "¿Vives cerca?" in out
    assert "Qué bien." in out  # las afirmaciones se conservan


def test_limitar_preguntas_afirmaciones_intactas() -> None:
    txt = "Claro que sí. Te explico. ¿Te late?"
    out = limitar_preguntas(txt, maximo=1)
    assert out.count("?") == 1
    assert "Claro que sí. Te explico." in out


def test_limitar_preguntas_config_dos() -> None:
    txt = "Hola. ¿Una? ¿Dos? ¿Tres?"
    assert limitar_preguntas(txt, maximo=2).count("?") == 2


def test_sanear_texto_libre_combinado() -> None:
    """Venezolanismo + 2 preguntas → sin venezolanismo y 1 sola pregunta."""
    txt = "¡Hola! ¿Está en alguna escuela? ¿Cómo lo viven?"
    out = sanear_texto_libre_haiku(txt, max_preguntas=1)
    assert "cómo lo viven" not in out.lower()
    assert out.count("?") == 1
    assert "¿Está en alguna escuela?" in out


def test_guards_no_tocan_texto_vacio() -> None:
    assert sanear_texto_libre_haiku("") == ""
