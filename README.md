# Sofía 2.0 — Maple Collège

Embajadora digital de admisiones de [Maple Collège](https://maplesaltillo.com). Reescritura en Python del agente que hoy vive en n8n, con arquitectura pensada para producción seria, separación de capas (identidad / reglas / datos volátiles / conocimiento), validators determinísticos y migración no destructiva.

**Estado actual:** Bloque 1 (scaffolding) completado.

## Stack

- **Python 3.11+** con `uv` para gestión de dependencias
- **FastAPI** (async) para webhooks y endpoints admin
- **Claude Haiku 4.5** como cerebro principal · GPT-4o-mini auxiliar · Whisper para audio
- **Supabase Postgres + pgvector** para memoria, KB y datos volátiles
- **Redis** para debounce 7s
- **Evolution API** (WhatsApp) · **Telegram Bot API** · **Web Chat con SSE** (multi-canal)
- **GitHub Actions** para CI · **EasyPanel + Docker** para deploy

## Setup local

```bash
# 1. Instalar uv si no lo tienes
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clonar e instalar dependencias
git clone https://github.com/rrintecai-sudo/sofia-maple.git
cd sofia-maple
uv sync

# 3. Configurar entorno
cp .env.example .env
# Edita .env con tus credenciales reales

# 4. Aplicar migraciones a Supabase (necesitas SUPABASE_DB_URL en .env)
uv run python scripts/apply_migrations.py

# 5. Arrancar la app local
uv run uvicorn app.main:app --reload

# 6. Verificar
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

## Setup vía Docker

```bash
docker compose up --build
# La app queda en http://localhost:8000
# Redis local arranca en :6379
```

## Tests

```bash
uv run pytest                  # tests unitarios + integración (sin golden)
uv run pytest -m golden        # golden conversations (lentos, costosos)
uv run ruff check .            # lint
uv run mypy app                # type check
```

## Estructura

Ver `docs/ARCHITECTURE.md` (fuente de verdad técnica). Resumen:

```
app/
  api/               webhooks (whatsapp, telegram, web, admin, health)
  core/              orchestrator, state, prompts modulares, validators
  tools/             kb_search, precios, horarios, calendar, send_image
  adapters/          anthropic, openai, supabase, postgres, redis, channels
  ingest/            pipeline de KB (PDF → chunks → embeddings)
  observability/    logger, costs, metrics
migrations/          SQL idempotentes para Supabase
tests/               unit + integration + golden conversations
docs/                ARCHITECTURE, DECISIONS, DEPLOYMENT, PROMPTS_GUIDE
```

## Documentos clave

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — decisiones técnicas + diseño completo
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — bitácora de decisiones tomadas durante implementación
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — guía de deploy a EasyPanel
- [`docs/PROMPTS_GUIDE.md`](docs/PROMPTS_GUIDE.md) — cómo editar y versionar los prompts modulares
- [`docs/KB_GUIDE.md`](docs/KB_GUIDE.md) — cómo añadir documentos a la base de conocimiento

## Principios no negociables (de Cecilia Trujillo, Maple Collège)

- ❌ La IA **NO** interactúa directamente con niños
- ❌ La IA **NO** toma decisiones disciplinarias
- ❌ La IA **NO** comunica decisiones finales a familias
- ✔ La IA apoya, analiza, sugiere y alerta
- ✔ Toda decisión final es humana

## Operador

RR INTEC AI Solutions · Owner técnico: Oscar Rodríguez
