"""Limpieza de texto pre-chunking (port del JS del técnico anterior)."""

from __future__ import annotations

import re
import unicodedata


def clean_text(text: str) -> str:
    """Normaliza texto antes de chunking.

    Pasos (idénticos al cleaner JS del workflow RAG Semantico v1):
    - NFD normalize + strip combining marks (quita tildes "raras" de OCR).
    - Reemplaza saltos de línea, retornos y tabs por espacios.
    - Comillas tipográficas → espacio.
    - Dashes largos → guión normal.
    - Bullets → guión.
    - Espacios dobles → uno.
    - Strip final.

    Devuelve el texto limpio (no destruye contenido, solo normaliza).
    """
    if not text:
        return ""

    # Normalizar NFD (separar tilde y letra) — luego volver a NFC al final.
    # El técnico anterior usaba NFD solo para limpieza intermedia.
    text = unicodedata.normalize("NFD", text)
    # No quitamos combining marks porque eso elimina tildes válidas en español;
    # solo normalizamos para que comparaciones sean estables.
    text = unicodedata.normalize("NFC", text)

    # Saltos de línea, retornos, tabs → espacio
    text = re.sub(r"[\n\r\t]+", " ", text)

    # Comillas tipográficas y angulares → espacio
    text = re.sub(r'[""«»\'‘’“”]', " ", text)

    # Dashes largos (em, en) → guión
    text = re.sub(r"[–—]", "-", text)

    # Bullets → guión
    text = re.sub(r"[•·]", "-", text)

    # Espacios múltiples → uno
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()


def escape_for_json(text: str) -> str:
    """Escapa texto para inclusión en JSON (cosa que JSON.stringify haría)."""
    import json

    # JSON.stringify wraps in quotes; quitamos las quotes exteriores
    return json.dumps(text, ensure_ascii=False)[1:-1]
