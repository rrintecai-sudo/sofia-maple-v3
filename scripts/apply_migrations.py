#!/usr/bin/env python
"""Aplica las migraciones SQL de `migrations/` a Supabase.

Dos backends soportados (intenta en orden):
  1. Management API con SUPABASE_PAT (preferido) — `POST /v1/projects/{ref}/database/query`
  2. Conexión directa con SUPABASE_DB_URL (asyncpg)

Uso:
    uv run python scripts/apply_migrations.py
    uv run python scripts/apply_migrations.py --dry-run

Las migraciones son idempotentes (CREATE TABLE IF NOT EXISTS, etc.) — se pueden
correr varias veces sin problema.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def list_migrations() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"⚠️  No hay archivos .sql en {MIGRATIONS_DIR}", file=sys.stderr)
    return files


async def apply_via_management_api(
    files: list[Path],
    project_ref: str,
    pat: str,
    dry_run: bool,
) -> None:
    """Aplica migraciones vía Management API. Usa el endpoint database/query."""
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            if dry_run:
                print(f"[DRY-RUN] {path.name} ({len(sql)} bytes)")
                continue
            print(f"→ aplicando {path.name} ...", end="", flush=True)
            resp = await client.post(url, headers=headers, json={"query": sql})
            if resp.status_code >= 400:
                print(" ❌")
                print(f"  HTTP {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
                raise SystemExit(1)
            print(" ok")


async def apply_via_asyncpg(files: list[Path], dsn: str, dry_run: bool) -> None:
    """Fallback: conexión directa con asyncpg."""
    import asyncpg

    conn = await asyncpg.connect(dsn=dsn)
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            if dry_run:
                print(f"[DRY-RUN] {path.name} ({len(sql)} bytes)")
                continue
            print(f"→ aplicando {path.name} ...", end="", flush=True)
            await conn.execute(sql)
            print(" ok")
    finally:
        await conn.close()


async def main(dry_run: bool) -> int:
    settings = get_settings()
    files = list_migrations()
    if not files:
        return 0

    # Preferir Management API si hay PAT
    if settings.supabase_pat and settings.supabase_project_ref:
        print(f"Aplicando vía Management API (project_ref={settings.supabase_project_ref})")
        await apply_via_management_api(
            files,
            project_ref=settings.supabase_project_ref,
            pat=settings.supabase_pat,
            dry_run=dry_run,
        )
    elif settings.supabase_db_url:
        print("Aplicando vía asyncpg (SUPABASE_DB_URL)")
        await apply_via_asyncpg(files, dsn=settings.supabase_db_url, dry_run=dry_run)
    else:
        print(
            "❌ No hay credenciales para DDL.\n"
            "   Configura SUPABASE_PAT + SUPABASE_PROJECT_REF (recomendado),\n"
            "   o SUPABASE_DB_URL en el .env.",
            file=sys.stderr,
        )
        return 2

    print(f"\n✅ {'Dry-run de' if dry_run else 'Aplicadas'} {len(files)} migraciones.")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra qué archivos se aplicarían sin ejecutarlos",
    )
    args = parser.parse_args()
    return asyncio.run(main(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(cli())
