"""Pipeline de ingesta de KB.

Convierte PDF/Markdown/DOCX en chunks embebidos en `documents_maple` de Supabase.

Pipeline (port del workflow `RAG Semantico` de la Sofia v1 n8n):
1. Extracción de texto del archivo.
2. Limpieza (tildes NFD, comillas tipográficas, bullets, dashes).
3. Análisis estructural con Claude Haiku 4.5 → JSON jerárquico (title/sections/subsections).
4. Chunking semántico (≥3 frases o >500 chars, port del JS del técnico anterior).
5. Clasificación de cada chunk a su sección con Claude Haiku 4.5.
6. Embedding con OpenAI text-embedding-3-small (1536 dims).
7. INSERT en documents_maple via PostgREST.

Uso CLI:
    uv run python -m app.ingest.pipeline --file ruta/al/doc.pdf
"""

from app.ingest.chunker import semantic_chunks
from app.ingest.cleaner import clean_text

__all__ = ["clean_text", "semantic_chunks"]
