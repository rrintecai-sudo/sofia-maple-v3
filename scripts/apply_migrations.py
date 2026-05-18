#!/usr/bin/env python
"""Aplica las migraciones SQL de `migrations/` a Supabase.

Uso:
    uv run python scripts/apply_migrations.py
    uv run python scripts/apply_migrations.py --dry-run

Requisito: SUPABASE_DB_URL en .env.
Saca la URL de Supabase Dashboard → Database → Connection String → URI.

Las migraciones son idempotentes (CREATE TABLE IF NOT EXISTS, etc.) — se pueden
correr varias veces sin problema.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def list_migrations() -> list[Path]:
    """Devuelve los archivos .sql en orden alfabético (= orden de aplicación)."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"⚠️  No hay archivos .sql en {MIGRATIONS_DIR}", file=sys.stderr)
        return []
    return files


async def apply_one(conn: asyncpg.Connection, path: Path, dry_run: bool) -> None:
    sql = path.read_text(encoding="utf-8")
    if dry_run:
        print(f"[DRY-RUN] {path.name} ({len(sql)} bytes)")
        return
    print(f"→ aplicando {path.name} ...", end="", flush=True)
    try:
        await conn.execute(sql)
        print(" ok")
    except Exception as exc:
        print(f"\n❌ Error en {path.name}: {exc}", file=sys.stderr)
        raise


async def main(dry_run: bool) -> int:
    settings = get_settings()
    if not settings.supabase_db_url:
        print(
            "❌ SUPABASE_DB_URL no está configurada.\n"
            "   Ve a Supabase Dashboard → Project Settings → Database → "
            "Connection String → URI (modo 'Transaction'). Cópiala en .env.",
            file=sys.stderr,
        )
        return 2

    files = await list_migrations()
    if not files:
        return 0

    print(f"Conectando a Postgres ({settings.supabase_url})")
    conn = await asyncpg.connect(dsn=settings.supabase_db_url)
    try:
        for path in files:
            await apply_one(conn, path, dry_run=dry_run)
    finally:
        await conn.close()

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
