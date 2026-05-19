#!/usr/bin/env python
"""Carga datos iniciales en las tablas volátiles desde los CSVs en data/seed/.

Uso:
    uv run python scripts/seed_tables.py
    uv run python scripts/seed_tables.py --dry-run

Tablas:
- precios_por_nivel
- horarios_por_nivel
- modalidades_estancia
- campus
- becas

Es idempotente: usa upsert con on_conflict. Si la fila existe, la actualiza;
si no, la crea. Los datos NO son los reales finales (placeholders razonables
basados en el prompt v2.8). Cecilia los confirma y se hace UPDATE SQL.

Acceso: Management API con SUPABASE_PAT (no necesita DB password).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


def _parse_csv_value(value: str) -> Any:
    """Convierte string CSV a valor SQL apropiado. Vacío → NULL."""
    value = value.strip()
    if value == "":
        return None
    # Tratar de detectar números
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass
    # Booleanos
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    # Arrays (PostgreSQL array literal: '{a,b,c}')
    if "," in value and not value.startswith("{"):
        # Si la columna es array-type, dejamos al caller convertirlo
        pass
    return value


def _format_sql_value(val: Any) -> str:
    """Formatea un valor para SQL string."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    # String — escapar comilla simple
    s = str(val).replace("'", "''")
    return f"'{s}'"


def _format_array(value: str) -> str:
    """Convierte 'a,b,c' a array literal PG: ARRAY['a','b','c']."""
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        return "ARRAY[]::TEXT[]"
    quoted = ", ".join(f"'{i}'" for i in items)
    return f"ARRAY[{quoted}]"


# Schemas — qué columnas son ARRAY de texto
ARRAY_COLUMNS: dict[str, set[str]] = {
    "campus": {"niveles"},
    "modalidades_estancia": {"aplica_para"},
}


def _build_upsert_sql(
    table: str,
    columns: list[str],
    rows: list[dict[str, str]],
    conflict_cols: list[str],
    extra_columns: dict[str, str] | None = None,
) -> str:
    """Construye un INSERT ... ON CONFLICT ... UPDATE SQL.

    extra_columns: columnas adicionales con valor literal (ej. ciclo_escolar='2026-2027')
    """
    extra = extra_columns or {}
    all_cols = list(columns) + list(extra.keys())

    values_clauses: list[str] = []
    array_cols = ARRAY_COLUMNS.get(table, set())
    for row in rows:
        parts: list[str] = []
        for col in columns:
            raw = row.get(col, "")
            if col in array_cols:
                parts.append(_format_array(raw))
            else:
                parts.append(_format_sql_value(_parse_csv_value(raw)))
        for lit in extra.values():
            parts.append(_format_sql_value(lit))
        values_clauses.append(f"({', '.join(parts)})")

    update_cols = [c for c in all_cols if c not in conflict_cols]
    update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
    quoted_all = ", ".join(f'"{c}"' for c in all_cols)
    quoted_conflict = ", ".join(f'"{c}"' for c in conflict_cols)

    sql = (
        f"INSERT INTO {table} ({quoted_all}) VALUES\n"
        + ",\n".join(values_clauses)
        + f"\nON CONFLICT ({quoted_conflict}) DO UPDATE SET "
        + update_set
        + ";"
    )
    return sql


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows = [{**row} for row in reader]
    return headers, rows


SEED_PLAN = [
    # (table, csv_file, conflict_cols, extra_columns)
    (
        "precios_por_nivel",
        "precios_2026-2027.csv",
        ["ciclo_escolar", "nivel", "sub_nivel"],
        {"ciclo_escolar": "2026-2027"},
    ),
    ("horarios_por_nivel", "horarios.csv", ["nivel", "modalidad"], None),
    ("campus", "campus.csv", ["nombre"], None),
    ("becas", "becas.csv", ["tipo"], None),
    (
        "modalidades_estancia",
        "modalidades_estancia_2026-2027.csv",
        ["ciclo_escolar", "nombre"],
        {"ciclo_escolar": "2026-2027"},
    ),
]


async def run_via_management_api(sql: str, settings: Any) -> None:
    """Ejecuta SQL vía la Management API (mismo path que apply_migrations)."""
    url = f"https://api.supabase.com/v1/projects/{settings.supabase_project_ref}/database/query"
    headers = {
        "Authorization": f"Bearer {settings.supabase_pat}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json={"query": sql})
        if resp.status_code >= 400:
            print(f"❌ HTTP {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
            raise SystemExit(1)


def _need_unique_constraints() -> list[str]:
    """SQL para asegurar UNIQUE constraints que necesita el ON CONFLICT.

    Las migraciones ya tienen UNIQUE en (ciclo_escolar, nivel, sub_nivel) y similares,
    pero `campus` y `becas` y `horarios_por_nivel` no tienen explícito. Lo agregamos
    idempotente.
    """
    return [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_campus_nombre ON campus(nombre);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_becas_tipo ON becas(tipo);",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_horarios_nivel_modalidad "
        "ON horarios_por_nivel(nivel, modalidad);",
    ]


async def main(dry_run: bool) -> int:
    settings = get_settings()
    if not settings.supabase_pat or not settings.supabase_project_ref:
        print(
            "❌ SUPABASE_PAT + SUPABASE_PROJECT_REF requeridos en .env",
            file=sys.stderr,
        )
        return 2

    # 1. Crear UNIQUE constraints necesarios
    print("→ asegurando UNIQUE constraints…")
    for stmt in _need_unique_constraints():
        if dry_run:
            print(f"  [DRY] {stmt}")
        else:
            await run_via_management_api(stmt, settings)
    print("  ok")

    # 2. Sembrar cada tabla
    for table, csv_file, conflict_cols, extra in SEED_PLAN:
        path = SEED_DIR / csv_file
        if not path.exists():
            print(f"⚠️  {csv_file} no existe, saltando")
            continue
        headers, rows = _read_csv(path)
        if not rows:
            print(f"⚠️  {csv_file} vacío, saltando")
            continue

        sql = _build_upsert_sql(
            table=table,
            columns=headers,
            rows=rows,
            conflict_cols=conflict_cols,
            extra_columns=extra,
        )
        if dry_run:
            print(f"\n[DRY-RUN] {table} ({len(rows)} filas)")
            print(sql[:400] + ("..." if len(sql) > 400 else ""))
        else:
            print(f"→ sembrando {table} ({len(rows)} filas)…", end="", flush=True)
            await run_via_management_api(sql, settings)
            print(" ok")

    print(f"\n✅ {'Dry-run de' if dry_run else 'Sembradas'} {len(SEED_PLAN)} tablas.")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(cli())
