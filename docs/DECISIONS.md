# Bitácora de decisiones — Sofía 2.0

Decisiones tomadas durante la implementación que no están explícitas en `ARCHITECTURE.md`. Cuando Claude Code toma un trade-off, se registra aquí.

Formato: `ADR-XXX — Título`, fecha, contexto, decisión, justificación, alternativas descartadas.

---

## ADR-001 — Docker local opcional, no requerido para verificación de Bloque 1

**Fecha:** 2026-05-18
**Contexto:** El plan de ejecución dice "verifica que `docker compose up` arranque". Docker Desktop no está instalado en el laptop de Oscar (macOS). Instalar Docker Desktop requiere intervención manual (GUI installer).
**Decisión:** Se entregan `Dockerfile` y `docker-compose.yml` validados sintácticamente. La verificación local del Bloque 1 se hace con `uv run uvicorn app.main:app`. La build Docker se valida en CI (GitHub Actions, que sí tiene Docker disponible).
**Justificación:** El criterio real de Bloque 1 es que la app arranque y responda `/healthz` y `/readyz`. Eso lo cubre uvicorn directo. Docker es un requisito de producción, no de desarrollo local. EasyPanel hará la build desde el repo.
**Alternativa descartada:** Pedir a Oscar que instale Docker Desktop antes de seguir — agrega fricción innecesaria para un check que CI puede hacer.

---

## ADR-002 — `/readyz` tolerante con Anthropic API sin key configurada

**Fecha:** 2026-05-18
**Contexto:** La API key de Anthropic se crea sólo en la web console (no hay CLI público). En Bloque 1 sólo necesitamos que la app arranque; la primera llamada real a Claude es en Bloque 2.
**Decisión:** `/readyz` reporta el estado de Anthropic como `"skip"` si `ANTHROPIC_API_KEY` está vacío, y como `"ok"` si la key existe y un `models` request responde (200 o 401 cuentan como reachable). Supabase, Redis y OpenAI sí son obligatorios para que `/readyz` retorne 200.
**Justificación:** Permite arrancar el Bloque 1 sin bloquear por algo que Oscar puede aportar antes del Bloque 2.
**Alternativa descartada:** Crear la key automáticamente — no hay API pública de Anthropic para esto.

---

## ADR-003 — `asyncpg` para acceso directo a Postgres, `supabase-py` solo para PostgREST específico

**Fecha:** 2026-05-18
**Contexto:** El stack usa Supabase, pero la mayoría de operaciones son SQL directo (memoria, turn_logs, datos volátiles, pgvector). `supabase-py` envuelve PostgREST que añade overhead.
**Decisión:** Cliente principal de Postgres es `asyncpg` con pool. Se usa `supabase-py` sólo si en algún momento necesitamos features específicos como Storage, Realtime o Auth.
**Justificación:** asyncpg es el cliente Postgres más rápido en Python. PostgREST no permite DDL ni queries complejas con `pgvector::similarity` cómodamente.

---

## ADR-004 — Logger JSON con stdlib, sin structlog ni vendor SDK

**Fecha:** 2026-05-18
**Contexto:** El stack debe ser mínimo. Necesitamos logs estructurados pero no queremos pagar el costo de dependencias adicionales si stdlib alcanza.
**Decisión:** Formatter JSON con `logging` de stdlib. Output a stdout (capturado por Docker/EasyPanel).
**Justificación:** stdlib es suficiente, cero deps extras, JSON parseable por cualquier agregador. Si en el futuro queremos Logfire o similar, el sink se cambia sin tocar el resto.

---

## ADR-005 — Tarifas de modelo en `app/observability/costs.py`, no en .env

**Fecha:** 2026-05-18
**Contexto:** Los precios de las APIs cambian. ¿Variables de entorno o constantes?
**Decisión:** Constantes en código, versionadas en Git. Cuando un proveedor cambia precio, se hace PR.
**Justificación:** Los precios cambian raramente (semestres). Tenerlos en código permite hacer diff cuando cambian. Tenerlos en .env hace que cada ambiente pueda tener números distintos, lo cual no tiene sentido.

---

## ADR-006 — Tests de adapters con mocks (httpx mock vía respx, fakeredis), no servicios reales en CI

**Fecha:** 2026-05-18
**Contexto:** Los tests unitarios no deben requerir Supabase real ni Anthropic real (costo, lentitud, flaky).
**Decisión:** Mocks por cliente. respx para httpx, fakeredis para Redis, monkeypatch para SDKs. Tests de integración con servicios reales viven bajo el marker `@pytest.mark.integration` y no corren en CI por default.
**Justificación:** CI rápido y barato. Tests de integración se corren manualmente o en nightly.

---

## ADR-007 — Migraciones vía Supabase Management API con PAT (preferido) + asyncpg fallback

**Fecha:** 2026-05-18
**Contexto:** Necesitamos aplicar DDL a Supabase. El `service_role` JWT autoriza PostgREST pero **no permite DDL**. Las dos vías legítimas son:
  - **Management API** (`POST /v1/projects/{ref}/database/query`) con Personal Access Token (PAT).
  - **Conexión directa Postgres** con `SUPABASE_DB_URL` y `asyncpg`.

**Decisión:** Migraciones como archivos `.sql` numerados, idempotentes (`CREATE TABLE IF NOT EXISTS`). `scripts/apply_migrations.py` intenta primero Management API (si hay `SUPABASE_PAT`), y fallback a `asyncpg` con `SUPABASE_DB_URL`.

**Justificación:** El PAT es la opción más cómoda porque (a) no expone DB password, (b) tiene scope acotado al proyecto, (c) se revoca con un click si se compromete. asyncpg queda como fallback para casos sin internet a la Management API.

**Verificación de aplicación (2026-05-18):** 3 migraciones aplicadas con éxito vía Management API, 10 tablas nuevas visibles vía PostgREST: `sofia_conversations`, `sofia_messages`, `sofia_turn_logs`, `precios_por_nivel`, `horarios_por_nivel`, `modalidades_estancia`, `campus`, `becas`, `sofia_feedback_pending`, `sofia_messages_legacy`.

---

## ADR-008 — Sin pre-commit hooks instalados automáticamente; sólo configuración

**Fecha:** 2026-05-18
**Contexto:** pre-commit requiere instalación local (`pre-commit install`). Forzarlo en el primer setup agrega fricción.
**Decisión:** Se entrega `.pre-commit-config.yaml` configurado. Quien quiera el hook corre `uv run pre-commit install` una vez.
**Justificación:** CI ya corre ruff y mypy. El hook local es comodidad opcional.

---
