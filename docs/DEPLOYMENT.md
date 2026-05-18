# Deployment — Sofía 2.0

> Este documento se completa al final del Bloque 6. Por ahora describe el setup local.

## Local

```bash
uv sync
cp .env.example .env  # rellenar valores
uv run uvicorn app.main:app --reload
```

## Docker local

```bash
docker compose up --build
```

## Producción (EasyPanel)

A documentar en Bloque 6.

## Aplicar migraciones

```bash
# Requiere SUPABASE_DB_URL en .env
uv run python scripts/apply_migrations.py
```

## Rollback

A documentar en Bloque 6.
