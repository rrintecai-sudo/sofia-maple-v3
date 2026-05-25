#!/usr/bin/env python
"""Ingesta atómica de `app/kb/niveles/*.md` a Supabase `documents_maple`.

Diferente del pipeline genérico (`app/ingest/pipeline.py`):
- 1 archivo .md = 1 fila en `documents_maple` (NO chunking adicional).
- El frontmatter YAML del archivo se mapea a `metadata` JSONB.
- El embedding se calcula sobre el contenido completo (sin frontmatter).
- Idempotente vía dedup por `metadata->>'source_file'`: borra filas previas
  del mismo source antes de insertar nuevas.

Uso:
    uv run python scripts/ingest_niveles_kb.py            # ingesta + verificación
    uv run python scripts/ingest_niveles_kb.py --dry-run  # solo parsea, no escribe
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
from app.adapters.openai_client import get_openai
from app.config import get_settings

KB_DIR = Path(__file__).resolve().parent.parent / "app" / "kb" / "niveles"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extrae frontmatter YAML simple de un .md. Devuelve (metadata, contenido)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    yaml_text, body = parts[1].strip(), parts[2].strip()

    # Parser YAML minimal (sin deps): clave: valor, listas con `-`
    metadata: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in yaml_text.splitlines():
        line_rstrip = line.rstrip()
        if not line_rstrip:
            continue
        if line_rstrip.startswith("  - ") or line_rstrip.startswith("    - "):
            # Item de lista
            if current_list_key is not None:
                metadata.setdefault(current_list_key, []).append(line_rstrip.split("- ", 1)[1])
            continue
        if ": " in line_rstrip:
            key, val = line_rstrip.split(": ", 1)
            key = key.strip()
            val = val.strip()
            if val == "":
                # Posible inicio de lista
                current_list_key = key
                metadata[key] = []
                continue
            current_list_key = None
            # Convertir tipos básicos
            if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
                metadata[key] = int(val)
            elif val.lower() == "true":
                metadata[key] = True
            elif val.lower() == "false":
                metadata[key] = False
            else:
                # Quitar comillas
                metadata[key] = val.strip("'\"")
    return metadata, body


async def embed_text(text: str) -> list[float]:
    """Embedding OpenAI text-embedding-3-small."""
    openai = get_openai()
    embs = await openai.embed([text])
    return embs[0]


async def delete_previous(settings: Any, source_marker: str) -> int:
    """Borra filas previas con metadata->>'source_marker' = source_marker."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{settings.supabase_url}/rest/v1/documents_maple",
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Prefer": "return=representation",
            },
            params={"metadata->>source_marker": f"eq.{source_marker}"},
        )
    if resp.status_code >= 400:
        print(f"⚠ DELETE failed HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 0
    deleted = resp.json() if resp.text else []
    return len(deleted)


async def insert_row(
    settings: Any, content: str, metadata: dict[str, Any], embedding: list[float]
) -> bool:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.supabase_url}/rest/v1/documents_maple",
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={"content": content, "metadata": metadata, "embedding": embedding},
        )
    if resp.status_code >= 400:
        print(f"❌ INSERT failed HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return False
    return True


SOURCE_MARKER = "kb_niveles_v1"


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.supabase_url:
        print("❌ SUPABASE_URL no configurado", file=sys.stderr)
        return 2

    files = sorted(KB_DIR.glob("*.md"))
    if not files:
        print(f"❌ No hay .md en {KB_DIR}", file=sys.stderr)
        return 2

    print(f"→ Parseando {len(files)} archivos...")
    items: list[tuple[Path, dict[str, Any], str]] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        metadata, body = parse_frontmatter(text)
        metadata["source_marker"] = SOURCE_MARKER
        metadata["source_file"] = f.name
        items.append((f, metadata, body))
        print(
            f"  ✓ {f.name:30s} → nivel={metadata.get('nivel', '?'):14s} cat={metadata.get('categoria', '?')}"
        )

    if args.dry_run:
        print("\n(dry-run) sin escribir a Supabase. Listo.")
        return 0

    # Limpiar filas previas
    print(f"\n→ Borrando filas previas con source_marker={SOURCE_MARKER}...")
    n_del = await delete_previous(settings, SOURCE_MARKER)
    print(f"  {n_del} filas eliminadas")

    # Embedding + insert
    print(f"\n→ Embedding + insertando {len(items)} filas...")
    inserted = 0
    for f, metadata, body in items:
        try:
            emb = await embed_text(body)
            ok = await insert_row(settings, body, metadata, emb)
            if ok:
                inserted += 1
                print(f"  ✓ {f.name}")
            else:
                print(f"  ✗ {f.name}")
        except Exception as exc:
            print(f"  ✗ {f.name}: {exc}", file=sys.stderr)

    print(f"\n✅ {inserted}/{len(items)} filas insertadas en documents_maple.")
    return 0 if inserted == len(items) else 1


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="No escribe a Supabase, solo parsea")
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(cli())
