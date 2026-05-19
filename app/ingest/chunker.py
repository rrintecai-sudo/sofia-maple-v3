"""Chunker semántico — port del JS de `RAG Semantico` v1.

Regla original (Semantic Chunker node):
- Split del texto por frases (regex `/(?<=[.?!])\\s+/`).
- Acumular frases hasta tener **≥3 frases O >500 caracteres**.
- Cada chunk lleva su `startIndex` (posición en el texto original).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Regex de fin de frase: punto/interrogación/exclamación + espacio
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")

MIN_SENTENCES_PER_CHUNK = 3
MIN_CHARS_PER_CHUNK = 500


@dataclass(frozen=True)
class Chunk:
    """Un chunk de texto semánticamente coherente."""

    text: str
    start_index: int | None  # posición en el texto original donde comienza


def semantic_chunks(
    text: str,
    *,
    min_sentences: int = MIN_SENTENCES_PER_CHUNK,
    min_chars: int = MIN_CHARS_PER_CHUNK,
) -> list[Chunk]:
    """Divide `text` en chunks semánticos.

    Regla: acumula frases hasta tener `min_sentences` frases O >`min_chars` chars.
    Si el texto es muy corto, retorna un único chunk con todo.

    Args:
        text: texto YA limpio (pasar por cleaner.clean_text primero).
        min_sentences: mínimo de frases por chunk antes de cortar.
        min_chars: mínimo de caracteres por chunk antes de cortar.

    Returns:
        Lista de Chunk con texto + start_index aproximado.
    """
    if not text:
        return []

    sentences = _SENTENCE_SPLIT_RE.split(text)
    if len(sentences) <= 1:
        # Texto sin puntuación clara — devolver como un solo chunk
        return [Chunk(text=text.strip(), start_index=0)]

    chunks: list[Chunk] = []
    current: list[str] = []
    char_cursor = 0

    for sentence in sentences:
        current.append(sentence)
        chunk_text = " ".join(current)

        if len(current) >= min_sentences or len(chunk_text) > min_chars:
            search_key = chunk_text[:30]
            start = text.find(search_key, char_cursor)
            start_index = start if start >= 0 else None
            chunks.append(Chunk(text=chunk_text, start_index=start_index))
            if start_index is not None:
                char_cursor = start_index + len(chunk_text)
            current = []

    # Frases que quedaron sin completar el umbral — último chunk
    if current:
        chunk_text = " ".join(current)
        search_key = chunk_text[:30]
        start = text.find(search_key, char_cursor)
        start_index = start if start >= 0 else None
        chunks.append(Chunk(text=chunk_text, start_index=start_index))

    return chunks
