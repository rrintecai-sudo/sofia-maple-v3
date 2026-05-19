"""Pipeline completo de ingesta: PDF/MD/TXT → chunks → embeddings → Supabase.

Uso CLI:
    uv run python -m app.ingest.pipeline --file ruta/al/doc.pdf [--title "..."] [--source-id "..."]

Soporta:
- `.pdf` (vía pypdf, opcional — degrada con mensaje si no hay deps)
- `.md` / `.txt` (lectura directa)

NO ingiere PDFs nuevos hasta que Cecilia nos pase material. El pipeline queda
listo para correrse cuando llegue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from app.adapters.anthropic_client import get_anthropic
from app.adapters.openai_client import get_openai
from app.config import get_settings
from app.ingest.chunker import Chunk, semantic_chunks
from app.ingest.cleaner import clean_text

log = logging.getLogger(__name__)


def extract_text_from_file(path: Path) -> str:
    """Extrae texto plano del archivo. Soporta .md, .txt, .pdf."""
    if path.suffix.lower() in (".md", ".txt"):
        return path.read_text(encoding="utf-8")

    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "Para PDFs necesitas pypdf: `uv add pypdf` y vuelve a correr."
            ) from exc
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)

    raise SystemExit(f"Formato no soportado: {path.suffix}")


async def analizar_estructura(texto: str) -> dict[str, Any]:
    """Llama a Claude Haiku para devolver la estructura jerárquica del documento.

    Devuelve dict tipo {title, sections: [{name, start, end, subsections: [...]}]}
    """
    anthropic = get_anthropic()
    if not anthropic.is_configured():
        log.warning("anthropic no configurado — saltando análisis estructural")
        return {"title": "documento sin título", "sections": []}

    prompt = (
        "Analiza el siguiente documento y devuélveme un resumen jerárquico en JSON "
        "estricto de su estructura. Para cada bloque, identifica:\n"
        '- "title": título general del documento\n'
        '- "sections": lista de secciones, cada una con "name", "start" (índice donde inicia), "end"\n'
        "Devuelve SOLO JSON, sin texto antes ni después.\n\n"
        "Texto:\n" + texto[:30000]  # cap por seguridad
    )

    settings = get_settings()
    msg = await anthropic.chat(
        system_blocks=[
            {"type": "text", "text": "Eres un extractor de estructura. Solo devuelves JSON válido."}
        ],
        messages=[{"role": "user", "content": prompt}],
        model=settings.anthropic_model_principal,
        max_tokens=2000,
        temperature=0.0,
    )
    raw = "".join(block.text for block in msg.content if hasattr(block, "text"))
    try:
        # Limpia code fences si llegaron
        cleaned = (
            raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        return json.loads(cleaned)
    except Exception as exc:
        log.warning("estructura inválida — usando default", extra={"error": str(exc)})
        return {"title": "documento", "sections": []}


def asignar_seccion(chunk: Chunk, estructura: dict[str, Any]) -> dict[str, str | None]:
    """Devuelve {section, subsection} para un chunk según su start_index."""
    if chunk.start_index is None:
        return {"section": None, "subsection": None}

    sections = estructura.get("sections") or []
    for section in sections:
        try:
            start = int(section.get("start", -1))
            end = int(section.get("end", -1))
        except (TypeError, ValueError):
            continue
        if start <= chunk.start_index <= end:
            sub = None
            for s in section.get("subsections") or []:
                try:
                    s_start = int(s.get("start", -1))
                    s_end = int(s.get("end", -1))
                except (TypeError, ValueError):
                    continue
                if s_start <= chunk.start_index <= s_end:
                    sub = s.get("name")
                    break
            return {"section": section.get("name"), "subsection": sub}

    return {"section": None, "subsection": None}


async def embed_chunks(textos: list[str]) -> list[list[float]]:
    """Embebe en batch los textos."""
    openai = get_openai()
    return await openai.embed(textos)


async def insertar_chunks_supabase(
    chunks: list[Chunk],
    metadatas: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> int:
    """Inserta los chunks en `documents_maple`. Retorna cuántos insertó."""
    settings = get_settings()
    if not settings.supabase_url:
        raise SystemExit("SUPABASE_URL no configurado")

    rows = [
        {"content": c.text, "metadata": m, "embedding": e}
        for c, m, e in zip(chunks, metadatas, embeddings, strict=True)
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.supabase_url}/rest/v1/documents_maple",
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=rows,
        )
    if resp.status_code >= 400:
        print(f"❌ HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        raise SystemExit(1)
    return len(rows)


async def ingest_file(
    file_path: Path,
    *,
    title_override: str | None = None,
    source_id: str | None = None,
    dry_run: bool = False,
) -> int:
    """Pipeline completo end-to-end. Retorna # chunks insertados."""
    print(f"→ extrayendo texto de {file_path.name}…")
    raw = extract_text_from_file(file_path)
    print(f"  {len(raw):,} chars")

    print("→ limpiando…")
    texto = clean_text(raw)

    print("→ analizando estructura con Claude Haiku…")
    estructura = await analizar_estructura(texto)
    titulo = title_override or estructura.get("title") or file_path.stem

    print("→ chunking semántico…")
    chunks = semantic_chunks(texto)
    print(f"  {len(chunks)} chunks generados")

    print("→ asignando secciones…")
    metadatas: list[dict[str, Any]] = []
    for chunk in chunks:
        sec_info = asignar_seccion(chunk, estructura)
        metadatas.append(
            {
                "title": titulo,
                "file_name": file_path.name,
                "section": sec_info["section"],
                "subsection": sec_info["subsection"],
                "id_file": source_id or file_path.stem,
                "pages": None,
            }
        )

    if dry_run:
        print(f"\n[DRY-RUN] insertaría {len(chunks)} chunks. Primer chunk:")
        print(f"  text[:200]: {chunks[0].text[:200]}")
        print(f"  metadata: {metadatas[0]}")
        return 0

    print(f"→ embedding {len(chunks)} chunks con OpenAI…")
    embeddings = await embed_chunks([c.text for c in chunks])

    print("→ insertando en Supabase documents_maple…")
    n = await insertar_chunks_supabase(chunks, metadatas, embeddings)
    print(f"✅ {n} chunks insertados.")
    return n


def cli() -> int:
    parser = argparse.ArgumentParser(description="Ingesta de KB para Sofía")
    parser.add_argument("--file", required=True, help="Ruta al archivo (.pdf, .md, .txt)")
    parser.add_argument("--title", help="Override del título (si el extractor no lo detecta bien)")
    parser.add_argument(
        "--source-id", help="ID de origen para metadata (default = stem del archivo)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Procesa sin insertar")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"❌ Archivo no existe: {path}", file=sys.stderr)
        return 2

    n = asyncio.run(
        ingest_file(
            file_path=path,
            title_override=args.title,
            source_id=args.source_id,
            dry_run=args.dry_run,
        )
    )
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(cli())
