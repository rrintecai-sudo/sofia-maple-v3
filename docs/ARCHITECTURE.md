# SOFIA 2.0 — Documento Maestro de Arquitectura

> **Cliente:** Maple Collège (Saltillo, Coahuila) — Cecilia Trujillo, Dirección General
> **Operador:** RR INTEC AI Solutions
> **Owner técnico:** Oscar Rodríguez
> **Documento para:** Oscar + Claude Code
> **Versión:** 1.1 · 18 de mayo de 2026
> **Estado:** Listo para ejecución
> **Cambios v1.0 → v1.1:** Añadida estrategia multi-canal (Web Chat + Telegram + WhatsApp), nuevos adapters, nuevo Sprint 3.5, actualizado plan de migración.

---

## 0. Cómo usar este documento

Este documento es la **fuente única de verdad** para reconstruir Sofía en código Python desde cero. No es un brief para Cecilia ni un documento comercial — es la guía técnica que tú vas a usar con Claude Code en la terminal para construir el sistema.

**Lectura recomendada:**

1. Lee secciones 1-3 (resumen, decisiones, principios) — entiendes el "qué" y el "por qué".
2. Hojea secciones 4-9 (arquitectura técnica) — sabes qué hay en cada lugar.
3. Cuando arranques con Claude Code, copia este archivo al repo (`/docs/ARCHITECTURE.md`) y referéncialo como contexto inicial.
4. Las secciones 10-14 son operativas — se leen al momento de ejecutar cada sprint.

Cuando una sección dice **"Para Claude Code"**, es una instrucción literal que se le copia al agente.

---

## 1. Resumen ejecutivo

**Qué es Sofía 2.0:**
Reconstrucción en Python de la embajadora digital de admisiones de Maple Collège que hoy vive en n8n. Misma identidad, mismo journey, mismo copy meticuloso de Emma/Gaby/Lily — diferente arquitectura técnica, pensada para producción seria y crecimiento sin deuda.

**Qué resuelve que la Sofía actual no resuelve:**

- Adherencia consistente a reglas largas (anti-repetición, anti-envío-fantasma, estado capturado).
- Separación entre identidad, reglas, conocimiento volátil y conocimiento cualitativo.
- Versionado profesional del prompt (Git, PRs, no editar 162KB de markdown en una UI).
- Test de regresión con las 186 conversaciones reales como golden set.
- Observabilidad por sesión y costos trazables.
- Base para escalar al resto del ecosistema Maple (Agente Académico, Comunicación Familias, dashboard).

**Qué NO es Sofía 2.0:**

- No es un agente autónomo con loop libre. Es un **workflow conversacional** con tools determinísticos.
- No usa frameworks pesados (LangChain, CrewAI). Llamadas directas a la API de Claude.
- No reemplaza a Lily ni al equipo humano. Es una extensión del journey hasta el agendado; el handoff a humano es parte del diseño.
- No interactúa con niños, no toma decisiones disciplinarias, no comunica decisiones finales a familias. Esos son los **principios no negociables de Cecilia** que se respetan en todos los agentes del ecosistema.

**Decisión clave de modelo:**
Claude Haiku 4.5 como cerebro principal, con arquitectura híbrida (GPT-4o-mini para clasificación, Whisper para voz). Costo estimado: ~$30–40/mes a volúmenes realistas. Adherencia a reglas complejas: significativamente mejor que GPT-mini, que es exactamente donde la Sofía actual falla.

**Decisión clave de paralelismo:**
La Sofía de n8n queda viva mientras Sofía 2.0 se construye y valida. Migración por fases, no big-bang. Equipo Maple no se entera del cambio de motor hasta que el motor nuevo esté probado.

**Decisión clave de canales (v1.1):**
Sofía 2.0 nace **multi-canal desde el día 1**: Web Chat (para QA interno y demos), Telegram (para probar audio, imágenes y experiencia móvil), y WhatsApp vía Evolution API (canal final de producción). Un solo cerebro, tres puertas de entrada. Esto evita el conflicto con la Sofía actual que ya usa el WhatsApp real de Maple, y permite iteración paralela sin riesgo.

---

## 2. Decisiones arquitectónicas y por qué

Esta sección documenta cada decisión técnica con su justificación. Son las decisiones que tú ya tomaste o que se derivan del análisis de la Sofía actual; quedan aquí para que Claude Code no las cuestione ni las re-litigue durante la implementación.

### 2.1 Lenguaje: Python

**Decisión:** Python 3.11+.

**Por qué:**
- Mantiene consistencia con tus otros agentes (Valentina, Carolina, Sofía-MAIN, Lexa Iuris, Asesor EyH).
- Ecosistema LLM/RAG maduro (anthropic SDK, openai SDK, supabase-py, FastAPI).
- Permite testing serio (pytest + golden tests).
- Reutilizable: lo que construyamos para Sofía sirve como base del Agente Académico y los demás.

### 2.2 Framework: ninguno pesado

**Decisión:** No LangChain, no LlamaIndex, no CrewAI, no LangGraph. **Llamadas directas al SDK de Anthropic + Supabase + Postgres.**

**Por qué:**
- Anthropic explícitamente recomienda "patrones simples y componibles" sobre frameworks complejos.
- Frameworks pesados ocultan lo que realmente está pasando. Para un agente con copy meticuloso, necesitas control fino.
- Menos dependencias = menos breakage cuando hay updates.
- Más fácil de leer y mantener para el equipo (tú + futuros colaboradores).

**Lo que sí se usa:** FastAPI (servidor web), pydantic (validación de tipos), httpx (HTTP async), supabase-py (cliente Supabase), anthropic (SDK oficial), openai (SDK oficial para auxiliares), psycopg (Postgres async), redis-py (debounce), pytest (testing).

### 2.3 Patrón: Workflow conversacional, no Agent loop

**Decisión:** Sofía es un **workflow** con pasos definidos por código, no un agente autónomo que decide qué tools llamar.

**Por qué:**
- Anthropic distingue Task / Workflow / Agent. Sofía cumple los criterios de Workflow: flujo conversacional con tools determinísticos, sin necesidad de razonamiento autónomo libre.
- Workflows son: más baratos, más predecibles, más fáciles de testear, más fáciles de versionar.
- El "Think tool" de la Sofía actual era un intento de simular agentic loop sobre n8n — costaba 3k tokens extra por turno y no resolvía las fallas de adherencia.

**Cómo se ve en código:** una función `procesar_turno(mensaje, session_id)` que orquesta: cargar estado → componer prompt → llamar Claude → validar respuesta → actualizar estado → enviar a WhatsApp. Sin loop interno, sin sub-agentes.

### 2.4 Modelo: Claude Haiku 4.5 (principal) + híbrido

**Decisión:** Claude Haiku 4.5 (`claude-haiku-4-5`) como cerebro principal de Sofía. GPT-4o-mini para clasificación trivial. Whisper para audio. OpenAI `text-embedding-3-small` para embeddings.

**Por qué Haiku 4.5 y no GPT-5-mini:**
- GPT-5-mini cuesta ~$17/mes a 300 papás; Haiku 4.5 cuesta ~$29/mes. Delta: ~$12/mes.
- Haiku 4.5 da mejor adherencia a reglas largas y complejas en español. Es exactamente donde la Sofía actual fallaba (repetición de siembras, evasión de preguntas, pérdida de estado).
- Haiku 4.5 logra ~90% del rendimiento de Sonnet 4.5 a un tercio del costo.
- Prompt caching de Anthropic (90% off en input cacheado) compensa fuerte el costo en arquitecturas con system prompt grande y repetido como la nuestra.

**Por qué no Sonnet 4.6:**
- Para conversación con papás, no hay tarea que requiera el premium de Sonnet. Si en QA detectamos casos donde Haiku falla y Sonnet acierta, se hace A/B y se decide con datos.

**Por qué no Opus 4.7:**
- 5x más caro que Haiku para un caso donde no necesitamos esa potencia. Opus se reserva para tareas analíticas complejas (eventualmente: Agente Académico evaluando planeaciones docentes).

**Por qué híbrido para tareas auxiliares:**
- Clasificación de intención (¿el papá pregunta costos, agenda, viene en frío?): GPT-4o-mini es 5x más barato y suficiente para una tarea de clasificación binaria/categórica.
- Audio (notas de voz del papá): Whisper es el estándar de mercado y sigue siendo lo mejor.
- Embeddings: `text-embedding-3-small` de OpenAI mantiene compatibilidad con la KB existente de la Sofía actual (que ya está en Supabase con ese modelo). No tiene sentido reembedderizar.

### 2.5 Stack de infraestructura

| Componente | Tecnología | Justificación |
|---|---|---|
| **API web** | FastAPI (async) | Estándar Python para webhooks, performante, tipado |
| **Base de datos relacional** | Supabase Postgres | Ya lo usas en 13+ proyectos, conocido |
| **Vector store** | Supabase pgvector | Mismo Postgres, sin servicio adicional |
| **Cola/debounce** | Redis | Ya está en el VPS, mismo patrón que la Sofía actual |
| **Canal WhatsApp** | Evolution API | Ya configurada, mantiene el número y el historial (canal final) |
| **Canal Telegram** | python-telegram-bot v21+ | Gratis, ideal para QA con audio/imagen, experiencia móvil real |
| **Canal Web Chat** | FastAPI + SSE (Server-Sent Events) | UI mínima embebida, iteración rapidísima en navegador |
| **Hosting** | VPS Hostinger (existente) | EasyPanel + Docker Compose, mismo stack |
| **Versionado** | GitHub privado | Repo nuevo dedicado |
| **CI/CD** | GitHub Actions → deploy a EasyPanel | Estándar, ya configurado en otros repos |
| **Observabilidad** | Logs estructurados a Supabase + Logfire (opcional) | Sin lock-in de vendor caro |
| **Tests** | pytest + golden conversations | 186 mensajes reales como fixtures |

### 2.6 Separación de capas (el principio más importante de todo el documento)

**Decisión:** El conocimiento de Sofía vive en **cuatro lugares físicamente separados**:

| Capa | Qué contiene | Dónde vive | Cómo se cambia |
|---|---|---|---|
| **Identidad** | Quién es Sofía, journey, principios, vocabulario, prohibiciones | `prompts/identity.md` y `prompts/journey/*.md` | PR en Git revisada por Oscar |
| **Reglas duras** | Anti-repetición, anti-envío-fantasma, captura de estado, guardarraíles | `core/validators.py` (código Python) | PR en Git con tests |
| **Conocimiento volátil** | Precios por nivel, horarios, modalidades, fechas límite, campus, gastos iniciales | Tablas Supabase (`precios_por_nivel`, `horarios`, etc.) | SQL update / panel admin |
| **Conocimiento cualitativo** | Filosofía Maple, modelo BEAR, metodologías, testimonios, FAQ ampliada | Embeddings en Supabase (`documents_maple`) | Subir PDF a Drive → pipeline ingesta |

**Por qué esto importa:** La Sofía actual mezcla las cuatro en un solo prompt de 1,200 líneas. Cualquier cambio de precio fuerza editar el prompt completo. Cualquier ajuste de tono se hace en el mismo lugar que datos factuales. Es insostenible.

Con esta separación:
- El equipo Maple puede pedir cambios de precio sin tocar código.
- El copy se versiona como código (PRs, blame, historial).
- Las reglas que el modelo no sigue bien se mueven a código determinístico.
- La KB crece sin riesgo de romper el prompt.

### 2.7 Estrategia multi-canal (nuevo en v1.1)

**Decisión:** Sofía 2.0 expone **tres canales** desde su primera versión funcional. Todos comparten el mismo orchestrator, mismo prompt, misma memoria. Lo único que cambia es el adapter de entrada/salida.

| Canal | Propósito | Cuándo se usa |
|---|---|---|
| **Web Chat** | QA interno, demos, iteración rápida | Sprints 1-9 (desarrollo) y siempre como herramienta interna |
| **Telegram** | QA con audio, imágenes, experiencia móvil real | Sprint 3.5 en adelante; reemplaza el chip dedicado de WhatsApp para tests |
| **WhatsApp (Evolution)** | Canal real de producción | Sprint 10 en adelante; reemplaza a la Sofía actual en Sprint 11 |

**Por qué multi-canal desde el día 1:**

1. **Bloquea el problema de convivencia con la Sofía actual.** No podemos usar el WhatsApp de Maple para QA — la actual ya lo usa. Telegram + Web nos dan tracks de prueba reales sin tocar producción.
2. **Telegram permite probar lo que el Web no puede:** audio (notas de voz reales), imágenes, notificaciones móviles, debounce real de mensajes en cadena. Eso es 80% de la experiencia WhatsApp.
3. **Web Chat es 10x más rápido para iterar copy.** Cambias un prompt, refrescas el navegador, pruebas. No esperas notificaciones, no consumes API limits de Telegram.
4. **Arquitectónicamente es barato:** el orchestrator ya es agnóstico al canal. Lo único que se duplica son los adapters de entrada y salida (~300 líneas de código por canal).
5. **Futuro:** si en Fase 2 algún agente Maple necesita ir por Discord, Slack, o un widget web del colegio, la arquitectura ya está lista.

**Lo que NO duplicamos por canal:**
- El cerebro (orchestrator).
- El prompt (es el mismo en los tres canales).
- La memoria (las conversaciones se guardan en `sofia_messages` con un campo `canal` que diferencia).
- Los validators (mismos para los tres).
- Los costos (se trackean igual).

**Lo que sí es específico de cada canal:**
- Cómo se recibe el webhook.
- Cómo se transcribe el audio (Telegram entrega .ogg, WhatsApp entrega base64 — ambos van a Whisper).
- Cómo se envía la respuesta (texto, imagen, sticker — cada canal tiene su API).
- Cómo se identifica al usuario (telegram_id, número WhatsApp, web_session_uuid).

---

## 3. Principios rectores

Estos principios resuelven decisiones que aparecerán durante la implementación. Cuando haya duda, se vuelve aquí.

### 3.1 Lo que prometió Cecilia (no negociable, hereda del brief original)

- ❌ La IA NO interactúa directamente con niños.
- ❌ La IA NO toma decisiones disciplinarias.
- ❌ La IA NO comunica decisiones finales a familias.
- ✔ La IA apoya, analiza, sugiere y alerta.
- ✔ Toda decisión final es humana.

Estos cinco puntos aplican a TODO el ecosistema Maple — Sofía es el primer agente, pero la promesa rige a todos los que vengan después.

### 3.2 Principios técnicos de implementación

1. **Determinístico cuando se pueda, LLM cuando se deba.** Si una validación puede ser código Python, no debe ser una regla en el prompt.
2. **Separación dura entre identidad, reglas, datos y conocimiento.** Ver sección 2.6.
3. **Tests antes de deploy.** Cada cambio de prompt o validator pasa por los golden tests de las 186 conversaciones.
4. **Costos por sesión visibles.** Cada conversación debe poder responder: ¿cuántos tokens consumió y cuánto costó?
5. **Sin sub-agentes para Sofía.** Si en el futuro necesitamos un sub-agente para algo específico, se justifica con datos, no por capricho arquitectónico.
6. **Migración no destructiva.** La Sofía de n8n no se apaga hasta que la nueva pase QA con tráfico real.

### 3.3 Principio de copy: respetar el alma

El copy actual (system prompt v2.8 + Think tool v2.1) es el resultado de iteración real de Emma, Gaby y Lily. Es **trabajo de marca** que NO se inventa de nuevo. Sofía 2.0 mantiene:

- Identidad y journey idénticos.
- Regla de Oro — Escena Observable.
- Protocolo neurodivergentes (3 pasos).
- Protocolo hijos en niveles distintos.
- Vocabulario oficial Maple vs prohibido.
- Manejo de objeciones específico por tipo.
- Modo Aprendizaje (`maple2026` / `/salir`).
- Las 13 frases de "munición" alta del journey.

Lo que cambia es **cómo se organiza ese copy** (modular en lugar de monolítico), **cómo se ejecuta** (determinístico cuando aplica), y **dónde se guarda** (Git en lugar de UI de n8n).

---

## 4. Estructura del repositorio

Para Claude Code: este es el árbol que vas a generar al hacer scaffolding del proyecto.

```
sofia-maple/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md        ← este documento, copiado al repo
│   ├── DEPLOYMENT.md          ← guía operativa de deploy
│   ├── PROMPTS_GUIDE.md       ← cómo editar/contribuir a los prompts
│   └── KB_GUIDE.md            ← cómo añadir documentos a la KB
├── pyproject.toml             ← dependencias (uv o poetry)
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
│
├── app/
│   ├── __init__.py
│   ├── main.py                ← FastAPI app
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── webhook_whatsapp.py ← POST /webhook/whatsapp (Evolution)
│   │   ├── webhook_telegram.py ← POST /webhook/telegram (Telegram Bot API)
│   │   ├── webhook_web.py      ← POST /webhook/web + SSE /chat/stream
│   │   ├── learning.py        ← endpoints Modo Aprendizaje (auth)
│   │   ├── admin.py           ← endpoints internos (stats, replay)
│   │   └── health.py          ← /healthz, /readyz
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── orchestrator.py    ← procesa_turno(msg, session_id)
│   │   ├── state.py           ← EstadoConversacion (pydantic model)
│   │   ├── prompts/
│   │   │   ├── identity.md
│   │   │   ├── rules.md
│   │   │   ├── journey/
│   │   │   │   ├── bienvenida.md
│   │   │   │   ├── descubrimiento.md
│   │   │   │   ├── educacion.md
│   │   │   │   ├── informacion.md
│   │   │   │   ├── objeciones.md
│   │   │   │   ├── agendado.md
│   │   │   │   └── post_agendado.md
│   │   │   ├── modo_aprendizaje.md
│   │   │   └── vocabulario.md
│   │   ├── prompt_builder.py  ← compone prompt según estado
│   │   ├── validators.py      ← anti-repetición, anti-envío-fantasma
│   │   ├── intent_classifier.py ← GPT-4o-mini para clasificar
│   │   └── modes.py           ← Modo Normal / Aprendizaje
│   │
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── kb_search.py       ← retrieve documents_maple (pgvector)
│   │   ├── precios.py         ← query tabla precios_por_nivel
│   │   ├── horarios.py        ← query tabla horarios
│   │   ├── calendar.py        ← Google Calendar para agendado
│   │   ├── send_image.py      ← envía imagen vía Evolution
│   │   └── send_sticker.py    ← envía sticker vía Evolution
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── anthropic_client.py ← wrapper con caching y retry
│   │   ├── openai_client.py
│   │   ├── supabase_client.py
│   │   ├── postgres_client.py
│   │   ├── redis_client.py
│   │   ├── channel.py          ← interfaz unificada: send_text/image/sticker, transcribe_voice
│   │   ├── evolution_client.py ← canal WhatsApp (Evolution API)
│   │   ├── telegram_client.py  ← canal Telegram (python-telegram-bot)
│   │   └── webchat_client.py   ← canal Web (SSE para streaming de respuesta)
│   │
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── pipeline.py        ← PDF → chunks → embeddings → Supabase
│   │   ├── chunker.py         ← semantic chunker (port del JS de n8n)
│   │   └── seed_tables.py     ← script para sembrar precios iniciales
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── logger.py          ← logging estructurado por session_id
│   │   ├── costs.py           ← cálculo de costos por turno
│   │   └── metrics.py
│   │
│   └── config.py              ← settings via pydantic-settings
│
├── web/                        ← UI mínima del Web Chat (servida por FastAPI)
│   ├── templates/
│   │   └── chat.html           ← single-page chat con SSE streaming
│   └── static/
│       ├── chat.css
│       └── chat.js
│
├── tests/
│   ├── conftest.py
│   ├── test_validators.py
│   ├── test_prompt_builder.py
│   ├── test_state.py
│   ├── test_intent_classifier.py
│   ├── golden/
│   │   ├── conversations/
│   │   │   ├── 5218441302112_2026-05-13.json
│   │   │   └── 34662236125_2026-05-13.json
│   │   ├── runner.py
│   │   └── assertions.py
│   └── integration/
│       └── test_webhook_e2e.py
│
├── scripts/
│   ├── import_n8n_history.py  ← importa chat_histories_sofia a la DB nueva
│   ├── replay_conversation.py ← replay de una conversación real contra el agente
│   └── compute_costs.py
│
└── migrations/                 ← Supabase SQL migrations
    ├── 001_init_schema.sql
    ├── 002_precios_horarios.sql
    └── 003_chat_messages.sql
```

**Notas clave para Claude Code:**

- Usa `uv` (no pip directo, no poetry) — es lo más rápido en 2026 para gestión de dependencias.
- Todo async (FastAPI async, httpx async, supabase async, psycopg async).
- pydantic v2 para todos los modelos de datos.
- pre-commit hooks: `ruff` (linter/formatter) + `mypy` (type checker).
- `.env` para secretos, nunca commiteados.

---

## 5. Esquema de datos (Supabase Postgres)

Esta sección define las tablas. Algunas existen ya (de la Sofía actual) y se reutilizan; otras son nuevas.

### 5.1 Tablas que se reutilizan de la Sofía actual

| Tabla | Estado | Uso en Sofía 2.0 |
|---|---|---|
| `documents_maple` | Existe, 16 chunks | KB cualitativa (filosofía Maple). Se va a ampliar. |
| `chat_histories_sofia` | Existe, 186 mensajes | Solo lectura — los importamos como golden tests. Sofía 2.0 escribe en tabla nueva. |

### 5.2 Tablas nuevas que se crean

#### `sofia_conversations`
Una fila por sesión (= número WhatsApp del papá).

```sql
CREATE TABLE sofia_conversations (
  session_id        TEXT PRIMARY KEY,            -- ver formato según canal abajo
  canal             TEXT NOT NULL,               -- 'whatsapp' | 'telegram' | 'web'
  identificador     TEXT NOT NULL,               -- número phone, telegram_id, o uuid web
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW(),
  estado_capturado  JSONB NOT NULL DEFAULT '{}', -- ver sección 6.2
  frases_usadas     TEXT[] DEFAULT '{}',         -- anti-repetición
  fase_journey      TEXT,                        -- bienvenida|descubrimiento|...
  agendado          BOOLEAN DEFAULT FALSE,
  fecha_agendado    TIMESTAMPTZ,
  modo              TEXT DEFAULT 'normal',       -- normal|aprendizaje
  notas_internas    TEXT,
  tester            BOOLEAN DEFAULT FALSE        -- TRUE si es Oscar/Lily/Gaby probando
);
CREATE INDEX idx_sofia_conv_updated ON sofia_conversations(updated_at DESC);
CREATE INDEX idx_sofia_conv_fase ON sofia_conversations(fase_journey);
CREATE INDEX idx_sofia_conv_canal ON sofia_conversations(canal);
```

**Formato de `session_id` por canal:**
- WhatsApp: `whatsapp:5218441302112@s.whatsapp.net`
- Telegram: `telegram:123456789` (chat_id de Telegram)
- Web: `web:<uuid_v4>` (generado en primera visita, persistido en cookie)

El prefijo del canal en el `session_id` evita colisiones y permite filtrar fácil.

#### `sofia_messages`
Una fila por mensaje (tanto del papá como de Sofía).

```sql
CREATE TABLE sofia_messages (
  id              BIGSERIAL PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sofia_conversations(session_id),
  role            TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
  content         TEXT NOT NULL,
  tipo            TEXT,                    -- 'texto' | 'audio' | 'imagen'
  metadata        JSONB DEFAULT '{}',      -- transcripción, descripción de imagen, etc.
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  tokens_input    INT,
  tokens_output   INT,
  cost_usd        NUMERIC(10,6),
  model_used      TEXT,
  cache_hit       BOOLEAN DEFAULT FALSE,
  latency_ms      INT
);
CREATE INDEX idx_sofia_msg_session ON sofia_messages(session_id, created_at);
CREATE INDEX idx_sofia_msg_created ON sofia_messages(created_at DESC);
```

#### `sofia_turn_logs`
Trazabilidad detallada de cada turno (para debug y observabilidad).

```sql
CREATE TABLE sofia_turn_logs (
  id              BIGSERIAL PRIMARY KEY,
  session_id      TEXT NOT NULL,
  turn_number     INT NOT NULL,
  user_message    TEXT,
  intent          TEXT,                    -- output del classifier
  rag_chunks      JSONB,                   -- chunks que se recuperaron
  tools_used      TEXT[],
  prompt_compuesto TEXT,                   -- el prompt EXACTO que se envió
  llm_response    TEXT,
  validators_passed JSONB,                 -- {anti_repeticion: true, ...}
  validators_failed JSONB,                 -- {anti_repeticion: "frase X"}
  final_response  TEXT,                    -- después de validators
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  tokens_input    INT,
  tokens_output   INT,
  cost_usd        NUMERIC(10,6),
  latency_ms      INT
);
CREATE INDEX idx_sofia_log_session ON sofia_turn_logs(session_id, turn_number);
```

#### `precios_por_nivel`
Conocimiento volátil — antes hardcoded en el prompt.

```sql
CREATE TABLE precios_por_nivel (
  id                  BIGSERIAL PRIMARY KEY,
  ciclo_escolar       TEXT NOT NULL,       -- "2026-2027"
  nivel               TEXT NOT NULL,       -- "kinder", "primaria", "secundaria"
  sub_nivel           TEXT,                -- "early_years", "preschool", etc.
  inscripcion         NUMERIC(10,2),
  colegiatura_mensual NUMERIC(10,2),
  recursos_educativos NUMERIC(10,2),
  seguro              NUMERIC(10,2),
  fecha_limite_pago   DATE,
  vigente             BOOLEAN DEFAULT TRUE,
  notas               TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (ciclo_escolar, nivel, sub_nivel)
);
```

#### `horarios_por_nivel`
```sql
CREATE TABLE horarios_por_nivel (
  id              BIGSERIAL PRIMARY KEY,
  nivel           TEXT NOT NULL,
  modalidad       TEXT,                    -- "regular", "extendido", "premater"
  hora_inicio     TIME,
  hora_fin        TIME,
  dias            TEXT,                    -- "L-V"
  notas           TEXT,
  vigente         BOOLEAN DEFAULT TRUE
);
```

#### `gastos_iniciales`, `campus`, `modalidades_estancia`, `becas`
Cada una su propia tabla. Estructura simple, una fila por concepto, columnas `vigente` y `notas`.

### 5.3 Migraciones

Para Claude Code: cada tabla en su archivo SQL bajo `migrations/`, numerados. Idempotentes (`CREATE TABLE IF NOT EXISTS`). Aplicarlos con un script `scripts/migrate.py` o vía Supabase CLI.

---

## 6. Diseño de prompts modulares

### 6.1 Inventario de archivos de prompt

| Archivo | Tamaño objetivo | Cuándo se carga |
|---|---|---|
| `identity.md` | ~2,500 tokens | SIEMPRE |
| `rules.md` | ~1,500 tokens | SIEMPRE (sin duplicar con identity) |
| `vocabulario.md` | ~500 tokens | SIEMPRE |
| `journey/bienvenida.md` | ~400 tokens | Solo si fase = bienvenida |
| `journey/descubrimiento.md` | ~600 tokens | Solo si fase = descubrimiento |
| `journey/educacion.md` | ~700 tokens | Solo si fase = educacion |
| `journey/informacion.md` | ~800 tokens | Solo si fase = informacion |
| `journey/objeciones.md` | ~900 tokens | Solo si hay objeción detectada |
| `journey/agendado.md` | ~500 tokens | Solo si fase = agendado |
| `journey/post_agendado.md` | ~300 tokens | Solo si agendado = true |
| `modo_aprendizaje.md` | ~600 tokens | Solo si modo = aprendizaje |

**Total siempre cargado:** ~4,500 tokens. Más una fase activa (~400-900 tokens). Total por turno: **~5,400-6,500 tokens de prompt**, vs. los **~40,000 actuales**. Reducción de ~85%.

### 6.2 Estructura de cada archivo

Cada `.md` empieza con un encabezado YAML para metadata:

```markdown
---
file: identity.md
version: 1.0
last_updated: 2026-05-18
load_when: always
estimated_tokens: 2500
---

# IDENTIDAD DE SOFÍA

Eres Sofía, la embajadora digital de admisiones de Maple Collège...

[resto del contenido]
```

### 6.3 Cómo se compone el prompt en runtime

`core/prompt_builder.py` expone:

```python
def build_system_prompt(estado: EstadoConversacion) -> str:
    """
    Compone el system prompt según el estado actual de la conversación.
    Retorna un string con prompt caching markers donde aplica.
    """
    sections = []
    sections.append(load_prompt("identity.md"))      # cacheable
    sections.append(load_prompt("rules.md"))         # cacheable
    sections.append(load_prompt("vocabulario.md"))   # cacheable
    
    if estado.fase_journey:
        sections.append(load_prompt(f"journey/{estado.fase_journey}.md"))
    
    if estado.modo == "aprendizaje":
        sections.append(load_prompt("modo_aprendizaje.md"))
    
    # Contexto dinámico (NO cacheable)
    sections.append(build_estado_capturado_section(estado))
    sections.append(build_datos_volatiles_section())  # precios actuales
    
    return "\n\n---\n\n".join(sections)
```

### 6.4 Prompt caching (esto es plata)

Anthropic permite marcar bloques como cacheables. Los bloques que se repiten turno a turno (identity, rules, vocabulario, journey activo) se marcan con `cache_control: {"type": "ephemeral"}`. Después del primer turno de una conversación, esos bloques cuestan 10% del precio.

Implementación en `adapters/anthropic_client.py`: cada bloque estable va como un `content block` separado con su `cache_control`. El SDK de Anthropic se encarga del resto.

**Impacto estimado:** ~$0.20/conversación → ~$0.097/conversación. Mitad del costo.

### 6.5 Migración del prompt actual

Para Claude Code: el prompt v2.8 actual está en el ZIP de Fernando (`maple-real/PROMPT_1_AI_Agent.md`, 1,202 líneas). Tarea de un sprint dedicado: dividir esas 1,202 líneas en los archivos modulares de arriba, eliminando duplicaciones (~25% del contenido es redundante).

**No se inventa nada. Se reorganiza.** El alma del copy (Regla de Oro, transiciones, journey) queda intacta.

---

## 7. Validators determinísticos

Esta es la sección más diferenciadora vs. la Sofía actual. Los validators son funciones Python que se ejecutan **después** de obtener la respuesta del LLM y **antes** de enviarla al papá. Si una validación falla, se regenera la respuesta (con un mensaje de feedback inyectado al prompt).

### 7.1 Anti-repetición de frases de munición

**Problema observado en producción:** Sofía sembró la frase "Aquí trabajamos muy de la mano con las familias…" dos veces en el mismo chat con 47 minutos de diferencia.

**Solución:**

```python
# core/validators.py

FRASES_MUNICION = [
    "Aquí trabajamos muy de la mano con las familias",
    "el desarrollo no pasa solo en el salón",
    "no formamos para el examen, formamos para la vida",
    # ... las 13 frases del journey
]

def validar_no_repeticion(respuesta: str, frases_ya_usadas: list[str]) -> ValidationResult:
    """Si la respuesta contiene una frase de munición ya usada, falla."""
    for frase in FRASES_MUNICION:
        if frase.lower() in respuesta.lower():
            if frase in frases_ya_usadas:
                return ValidationResult(
                    passed=False,
                    reason=f"Frase munición '{frase}' ya usada en este chat. Genera con variación."
                )
    return ValidationResult(passed=True)
```

Si falla, se regenera el turno con la instrucción "evita la frase X, ya la usaste".

### 7.2 Anti-envío-fantasma de imagen

**Problema observado:** Sofía dice "ya te envié la tabla de costos" sin haber llamado al tool de envío.

**Solución:** si la respuesta contiene patrones como "ya te envié", "te adjunto", "te mandé la imagen" y no se ejecutó la tool `send_image`, se reescribe la respuesta tachando esa frase.

### 7.3 Anti-pregunta-repetida

**Problema observado:** Sofía preguntó "¿está en alguna escuela?" 2 minutos después de que el papá ya había respondido "sí, está en otra escuela en 1° de primaria".

**Solución:** mantener `estado.datos_capturados` con campos tipados. Antes de enviar la respuesta, validar que no esté preguntando algo que ya está en el estado.

```python
class EstadoCapturado(BaseModel):
    nombre_papa: str | None = None
    nombre_hijo: str | None = None
    edad_hijo: int | None = None
    nivel_buscado: str | None = None
    escuela_actual: str | None = None
    miedos: list[str] = []
    resono_con: list[str] = []
    presupuesto_mencionado: bool = False
    pidio_costos: bool = False
    cita_agendada: bool = False
```

Cada turno, un extractor (GPT-4o-mini o el mismo Haiku con structured output) actualiza el estado. El prompt se compone inyectando explícitamente "ya sabes: nivel=primaria, edad=8, escuela_actual=sí (otra escuela)".

### 7.4 Anti-evasión de pregunta directa

Si el papá pregunta algo concreto ("¿cuánto cuesta?") y la respuesta de Sofía no contiene ni números ni la frase "déjame confirmar", se considera evasión y se regenera.

### 7.5 Validación de tono (post-MVP)

Para post-MVP: clasificador rápido (GPT-4o-mini) que verifica que el tono de la respuesta sea consistente con la guía Maple (cordial, sin urgencia, sin vendedor). Si detecta tono inconsistente, regenera.

### 7.6 Política de regeneración

- Máximo **2 regeneraciones** por turno. Si tras 2 intentos sigue fallando, se envía la última versión y se loggea como `validator_warning` (revisar después).
- Cada regeneración cuesta tokens. El budget de un turno es ~3 llamadas LLM máximo (1 + 2 regen).

---

## 8. Pipeline de ingesta de KB

La KB actual tiene 16 chunks de 1 PDF. Esto es ridículamente poco. Sofía 2.0 va a tener una KB mucho más rica.

### 8.1 Documentos a ingerir (priorizado)

| Documento | Fuente | Prioridad |
|---|---|---|
| Guía "Una guía desde el corazón…" (existente) | Drive `Sistema RAG` | Ya está, mantener |
| Manual de padres completo de Maple | Pedir a Cecilia | Alta |
| FAQ de admisiones (Lily la tiene) | Pedir a Lily | Alta |
| Descripción detallada por nivel (Maternal, Preschool, Primaria, Secundaria) | Pedir a Cecilia | Alta |
| Calendario escolar 2026-2027 | Pedir a Cecilia | Media |
| Política de uniforme | Pedir a Cecilia | Media |
| Testimonios reales de padres | Pedir a Cecilia | Media |
| Casos de neurodivergentes (anonimizados) | Pedir a Cecilia | Baja (pero alto valor) |

### 8.2 Pipeline (port del JS de n8n a Python)

`app/ingest/pipeline.py`:

```
Input: PDF/DOCX/MD en local o Drive
  ↓
1. Extracción de texto (pypdf / python-docx / markdown)
  ↓
2. Limpieza (normalización tildes, comillas, bullets)
  ↓
3. Análisis estructural con Claude Haiku 4.5
   → JSON con title/sections/subsections (start/end indices)
  ↓
4. Chunking semántico
   → ≥3 frases o >500 chars
  ↓
5. Clasificación de cada chunk a su sección
   → JSON {title, section, subsection}
  ↓
6. Embedding con OpenAI text-embedding-3-small (dimensions=1536)
  ↓
7. Insert en documents_maple
```

**Reutilizamos el código JS de Fernando** como referencia (está en `rag_semantico.json` nodo `Semantic Chunker`). Lo portamos a Python con cuidado de mantener el mismo comportamiento.

### 8.3 Trigger

Dos opciones:

- **Manual:** un script CLI `python -m app.ingest.pipeline --file ruta/al.pdf`.
- **Automático:** Google Drive folder watcher (como hace la Sofía actual). Polling cada 5 min.

Para MVP, manual es suficiente. Automático es post-MVP.

---

## 9. Endpoints FastAPI

### 9.1 Webhooks por canal

Cada canal tiene su propio webhook. Los tres terminan llamando al mismo `orchestrator.procesar_turno()`.

**WhatsApp (Evolution API):**
```
POST /webhook/whatsapp
```
Recibe el payload de Evolution. Implementa:
1. Parse del mensaje (texto / voz / imagen).
2. Si voz → Whisper → texto.
3. Si imagen → GPT-4o-mini vision → descripción.
4. Push a Redis con `session_id` y wait 7s (debounce).
5. Tras el wait, concatena mensajes y procesa.
6. Llama `orchestrator.procesar_turno(mensaje, session_id="whatsapp:...")`.
7. Envía respuesta vía Evolution API.

**Telegram (Bot API):**
```
POST /webhook/telegram
```
1. Parse del Update de Telegram (text, voice, photo, document).
2. Si voice → descarga .ogg → Whisper → texto.
3. Si photo → descarga → GPT-4o-mini vision.
4. Debounce con Redis (también 7s, mismo patrón).
5. Llama `orchestrator.procesar_turno(mensaje, session_id="telegram:<chat_id>")`.
6. Envía respuesta vía Telegram Bot API (sendMessage / sendPhoto / sendSticker).
7. Soporta typing indicators (sendChatAction) para UX más natural.

**Configuración de Telegram:**
- Bot creado en @BotFather.
- Webhook configurado vía `setWebhook` apuntando a `https://<tu-vps>/webhook/telegram`.
- Token guardado en `.env` como `TELEGRAM_BOT_TOKEN`.

**Web Chat:**
```
GET  /chat                    → sirve chat.html (la UI)
POST /webhook/web             → recibe un mensaje del usuario
GET  /chat/stream/{session_id} → SSE (Server-Sent Events) para streaming de respuesta
```

1. Usuario abre `/chat` en el navegador. Cookie con `web_session_uuid` si no existe.
2. Escribe mensaje → POST /webhook/web con `{session_id, content}`.
3. Backend procesa con orchestrator (sin debounce — el web no manda mensajes en cadena).
4. Respuesta vuelve por SSE para dar efecto de streaming (Claude soporta streaming nativo).
5. Frontend renderiza tokens conforme llegan.

**Sin audio/imagen en web inicialmente.** Si en el futuro quieres soportarlo, se agregan inputs al HTML.

### 9.2 Modo Aprendizaje

```
POST /webhook/whatsapp con mensaje == "maple2026" → activa modo
POST /webhook/whatsapp con mensaje == "/salir"   → vuelve a normal
```

**Mejora vs. la actual:** el feedback se registra pero **no se aplica automáticamente al prompt**. Se guarda en una tabla `sofia_feedback_pending` y un humano (Oscar) lo revisa, lo aprueba/rechaza, y si aprueba se crea un PR al `rules.md` o donde corresponda.

```
GET  /admin/feedback/pending      → listar feedback no revisado
POST /admin/feedback/{id}/approve → genera PR
POST /admin/feedback/{id}/reject  → archiva
```

### 9.3 Admin y observabilidad

```
GET /admin/conversations           → listar conversaciones recientes
GET /admin/conversations/{id}      → ver historial completo de una conversación
GET /admin/turn-logs/{conv_id}     → ver el trace de cada turno (prompt enviado, etc.)
GET /admin/stats                   → métricas: # conversaciones, # agendados, costo total
GET /admin/costs?from=...&to=...   → costos por periodo
POST /admin/replay                 → re-correr una conversación contra una versión nueva de prompt
```

Protegido con API key simple (header `X-Admin-Key`) — no es API pública, solo para ti.

### 9.4 Health

```
GET /healthz  → 200 si la app está viva
GET /readyz   → 200 si puede llegar a Supabase, OpenAI, Anthropic, Redis, Evolution
```

### 9.5 Interfaz unificada de canal

Para que el orchestrator no le importe por qué canal vino el mensaje, se define una interfaz común en `adapters/channel.py`:

```python
class Channel(Protocol):
    name: str  # 'whatsapp' | 'telegram' | 'web'

    async def send_text(self, session_id: str, text: str) -> None: ...
    async def send_image(self, session_id: str, image_url: str, caption: str | None = None) -> None: ...
    async def send_sticker(self, session_id: str, sticker_id: str) -> None: ...
    async def transcribe_voice(self, voice_payload: dict) -> str: ...  # cada canal entrega diferente
    async def describe_image(self, image_payload: dict) -> str: ...
    async def mark_as_read(self, session_id: str, message_id: str) -> None: ...
    async def typing_indicator(self, session_id: str, on: bool = True) -> None: ...
```

Cada adapter (`EvolutionChannel`, `TelegramChannel`, `WebChannel`) implementa esta interfaz. El orchestrator recibe la instancia del canal correcto según el prefijo del `session_id`:

```python
def get_channel(session_id: str) -> Channel:
    if session_id.startswith("whatsapp:"):
        return EvolutionChannel(...)
    elif session_id.startswith("telegram:"):
        return TelegramChannel(...)
    elif session_id.startswith("web:"):
        return WebChannel(...)
```

**Capacidades por canal (qué soporta cada uno):**

| Capacidad | WhatsApp | Telegram | Web |
|---|:---:|:---:|:---:|
| Texto | ✅ | ✅ | ✅ |
| Audio entrante (Whisper) | ✅ | ✅ | ❌ MVP |
| Imagen entrante (vision) | ✅ | ✅ | ❌ MVP |
| Enviar imagen | ✅ | ✅ | ✅ |
| Enviar sticker | ✅ | ✅ | ⚠️ emoji |
| Debounce 7s | ✅ | ✅ | ❌ (innecesario) |
| Streaming token-a-token | ❌ | ❌ | ✅ |
| Marcar leído | ✅ | ✅ | N/A |
| Typing indicator | ❌ | ✅ | ✅ |

---

## 10. Flujo completo de un turno

Para que quede absolutamente claro qué pasa cuando un papá manda un mensaje:

```
1. Papá envía "Hola, busco información de primaria"
   ↓
2. Evolution API → POST /webhook/whatsapp
   ↓
3. webhook.py: parse, push a Redis list "session:<id>", wait 7s
   ↓
4. Tras 7s: pop todos los mensajes de la ventana → concatenar
   ↓
5. orchestrator.procesar_turno():
   a. Cargar EstadoConversacion desde sofia_conversations
   b. Si no existe → crear (fase_journey = bienvenida)
   c. intent_classifier.clasificar(msg) → "saludo_inicial" + "pregunta_info_nivel"
   d. extractor.actualizar_estado(msg, estado) → estado.nivel_buscado = "primaria"
   e. prompt_builder.build_system_prompt(estado) → ~5,500 tokens
   f. Recuperar últimos 10-15 turnos de sofia_messages
   g. Llamar anthropic_client.chat(system, messages) con prompt caching
   h. Obtener respuesta cruda del LLM
   i. validators.validate_all(respuesta, estado) → pasa/falla
   j. Si falla → regenerar (max 2 veces)
   k. Si la respuesta requiere tool (send_image, calendar) → ejecutar
   l. Guardar mensaje en sofia_messages (con costos, tokens, modelo)
   m. Guardar turn_log en sofia_turn_logs
   n. Actualizar sofia_conversations (estado, fase, timestamps)
   ↓
6. evolution_client.send_text(session_id, respuesta_final)
   ↓
7. Marcar como leído
   ↓
8. Logger: structured log con session_id, latencia, costo
```

**Latencia objetivo:** P50 < 5 segundos (incluyendo los 7s de debounce → P50 efectivo < 12s desde que el papá manda hasta que recibe respuesta).

**Costo objetivo:** P50 < $0.01 por turno con caching activo.

---

## 11. Migración desde n8n (estrategia y pasos)

### 11.1 Principio rector de la migración

**La Sofía de n8n NO se apaga hasta que la nueva esté validada con tráfico real.** Cero tolerancia a perder un papá en plena conversación.

### 11.2 Fases de migración

**Fase 0 — Preparación (Sprint 0):**
- Importar las 186 conversaciones reales a una tabla nueva (`sofia_messages_n8n_legacy`) en Supabase.
- Estas se vuelven los golden tests.
- Extraer prompt v2.8 y dividirlo en archivos modulares (~25% se elimina por duplicación).

**Fase 1 — Sofía 2.0 corriendo en canales paralelos (Sprints 1-9):**
- Sofía 2.0 deployada y accesible vía **Web Chat** (desde Sprint 3) y **Telegram** (desde Sprint 3.5).
- La Sofía actual de n8n sigue intacta en WhatsApp, sin enterarse.
- Tú, Lily y Gaby iteran sobre el copy y los validators sin riesgo.
- Conversaciones de prueba se guardan en `sofia_messages` con `canal IN ('telegram','web')` y `tester = TRUE`.
- Pasar los golden tests (tasa de regresión < 5%).

**Fase 2 — Sofía 2.0 en WhatsApp sandbox (Sprint 10):**
- Levantamos una nueva instancia Evolution API con un número WhatsApp DEDICADO al QA (chip nuevo o número virtual ~$5 USD).
- Sofía 2.0 contesta en ese número.
- Pruebas finales de WhatsApp con tráfico simulado de papás (puede ser tú + Lily + Gaby + 2-3 papás colaborativos que sepan).
- La Sofía actual sigue intacta en el WhatsApp real.

**Fase 3 — Convivencia (inicio de Sprint 11):**
- Sofía 2.0 recibe el 10% del tráfico real del WhatsApp de Maple (regla simple en el webhook de Evolution, o duplicación del webhook con shadowing).
- Sofía vieja sigue respondiendo el 90%.
- Comparamos calidad: cuál respondería mejor a cada mensaje (Lily revisa una muestra).

**Fase 4 — Cambio de motor (final de Sprint 11):**
- Sofía 2.0 toma el 100% del tráfico.
- Sofía vieja queda apagada pero el workflow se conserva por 30 días como rollback.

**Fase 5 — Limpieza:**
- Pasados 30 días estables, se archiva el workflow de n8n.
- La KB existente (`documents_maple`) se mantiene y se va ampliando.
- Web Chat y Telegram quedan vivos como herramientas internas (QA de futuros cambios, demos a Cecilia).

### 11.3 Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Pérdida de continuidad de un papá durante el switch | Importar historial de `chat_histories_sofia` a `sofia_messages` antes del switch |
| Sofía 2.0 responde peor que la vieja | Fase 1 y 2 lo detectan antes del switch real |
| El equipo Maple no nota el cambio y se confunde | No avisamos al equipo hasta Fase 3; el cambio es invisible para ellos |
| Costos imprevistos en Anthropic | Dashboard de costos en /admin/costs + alertas a $50/mes y $100/mes |

---

## 12. Plan de delivery por sprints (para Claude Code)

Cada sprint es ~3-5 días de trabajo con Claude Code en la terminal. El orden importa: respeta las dependencias.

### Sprint 0 — Scaffolding y base (3 días)
- Crear repo `sofia-maple` en GitHub.
- Estructura del repo (sección 4).
- `pyproject.toml` con dependencias.
- Docker Compose con FastAPI + Redis local.
- Migraciones SQL de las tablas nuevas (sección 5).
- Cliente Supabase configurado.
- Health endpoints.
- CI básico (lint + tests vacíos).

**Entregable:** `docker compose up` arranca, `/healthz` responde 200.

### Sprint 1 — Adapters y observabilidad (3 días)
- `anthropic_client.py` con caching y retry.
- `openai_client.py`.
- `evolution_client.py` (envío de texto, imagen, sticker, marcar leído).
- `redis_client.py` (debounce).
- `logger.py` (structured logs).
- `costs.py` (cálculo de costo por turno con tarifas actualizadas).

**Entregable:** scripts CLI para probar cada adapter independientemente.

### Sprint 2 — Modelo de estado y prompts modulares (5 días)
- `state.py` con `EstadoConversacion` y `EstadoCapturado` pydantic.
- División del prompt v2.8 en archivos modulares bajo `prompts/`.
- `prompt_builder.py` que compone el prompt según estado.
- Tests unitarios del builder.

**Entregable:** dado un estado, `build_system_prompt(estado)` retorna el prompt correcto con caching markers.

### Sprint 3 — Orchestrator MVP + Web Chat (5 días)
- `intent_classifier.py` con GPT-4o-mini.
- Extractor de estado (también GPT-4o-mini con structured output).
- `orchestrator.py` con flujo completo (sin validators aún).
- `adapters/channel.py` (interfaz unificada).
- `adapters/webchat_client.py` (WebChannel).
- `web/templates/chat.html` + `chat.js` + `chat.css` (UI mínima de chat con SSE streaming).
- Webhook `/webhook/web` end-to-end.
- `GET /chat` que sirve la UI.
- Tests integración: enviar mensaje fake → respuesta válida.

**Entregable:** Oscar abre `https://<vps>/chat` en el navegador y conversa con Sofía 2.0 en tiempo real con streaming.

### Sprint 3.5 — Telegram + Audio + Imagen (4 días)
- `adapters/telegram_client.py` (TelegramChannel).
- Webhook `/webhook/telegram` con parse de Updates.
- Soporte de voz: descarga .ogg → Whisper → texto.
- Soporte de imagen: descarga → GPT-4o-mini vision → descripción.
- Debounce con Redis (7s, mismo patrón que WhatsApp).
- Typing indicator (`sendChatAction`).
- Bot creado en @BotFather con nombre `MapleSofiaBot` o similar.
- Configuración del webhook vía `setWebhook`.
- Test manual: Oscar, Lily y Gaby agregan el bot y prueban texto + audio + imagen.

**Entregable:** Sofía 2.0 conversando en Telegram con experiencia móvil real. Lily y Gaby pueden empezar a iterar sobre el copy.

### Sprint 4 — Validators (4 días)
- `validators.py` con las 4 validaciones (sección 7).
- Integración con el orchestrator (regeneración con feedback).
- Tests unitarios por validator.

**Entregable:** los bugs observados (repetición, envío fantasma, pregunta repetida) ya no ocurren en pruebas. Validación con tráfico real de Telegram + Web.

### Sprint 5 — Tools (3 días)
- `kb_search.py` (pgvector retrieval).
- `precios.py` y `horarios.py` (queries Supabase).
- `send_image.py` y `send_sticker.py` (usan la interfaz `Channel` — funciona en los 3 canales).
- `calendar.py` (Google Calendar OAuth).
- Seed inicial de tablas precios/horarios.

**Entregable:** Sofía puede consultar precios sin tenerlos hardcoded en el prompt.

### Sprint 6 — Modo Aprendizaje seguro (3 días)
- Branch en orchestrator para modo aprendizaje.
- Tabla `sofia_feedback_pending`.
- Endpoints admin para revisar/aprobar/rechazar.
- Sin auto-aplicación (PR manual).

**Entregable:** `maple2026` activa modo, feedback se guarda pendiente.

### Sprint 7 — Golden tests (4 días)
- Importar las 186 conversaciones a la DB.
- `golden/runner.py`: replay de conversaciones contra Sofía 2.0.
- `assertions.py`: medir qué % de respuestas son "equivalentes" a las reales (LLM-as-judge con Claude Sonnet 4.6).
- Reporte de regresión.

**Entregable:** test suite que corre en CI y reporta calidad.

### Sprint 8 — Ingesta de KB (3 días)
- Port del semantic chunker JS → Python.
- `pipeline.py` end-to-end.
- Script CLI para ingerir un PDF.
- Documentación de cómo añadir documentos.

**Entregable:** pipeline funcionando, KB lista para crecer.

### Sprint 9 — Admin dashboard mínimo (3 días)
- Endpoints `/admin/*`.
- (Opcional) UI simple con Next.js o solo HTML con jinja2 servido por FastAPI.

**Entregable:** Oscar puede ver conversaciones, costos y traces sin entrar a la DB.

### Sprint 10 — Conectar WhatsApp + QA exhaustivo (1 semana)
- `adapters/evolution_client.py` (EvolutionChannel completo).
- Webhook `/webhook/whatsapp` end-to-end.
- Crear nueva instancia Evolution API en el VPS apuntando a un número WhatsApp DEDICADO al QA (chip nuevo o número virtual).
- Importar las 186 conversaciones de la Sofía actual a `sofia_messages` legacy.
- QA del pipeline WhatsApp completo: audio, imagen, debounce 7s, stickers, marcado como leído.
- Lily y Gaby prueban exhaustivamente con tráfico simulado de papás.
- Bugfixes según hallazgos.

**Entregable:** Sofía 2.0 funcionando en WhatsApp con número de QA, indistinguible de la actual en experiencia, mejor en consistencia.

### Sprint 11 — Convivencia y switch al WhatsApp real de Maple (1 semana)
- Configurar Evolution para enrutar 10% del tráfico real al webhook de Sofía 2.0 (regla por phone hash o por horario).
- Sofía vieja (n8n) sigue respondiendo el 90%.
- Lily revisa una muestra de respuestas comparativas: ¿cuál hubiera respondido mejor?
- Si el comparativo es favorable → switch al 100%.
- Cambio del webhook de Evolution para apuntar 100% a Sofía 2.0.
- Workflow de n8n se DESACTIVA pero NO se borra (rollback disponible por 30 días).
- Monitoreo cerca de las primeras 72h (alertas a Oscar en cualquier anomalía).

**Entregable:** Sofía 2.0 atendiendo el 100% del WhatsApp real de Maple. n8n apagado pero preservado. Equipo Maple no debe notar diferencia, salvo que Sofía responde mejor.

---

## 13. Tests y golden conversations

### 13.1 Tipos de tests

| Tipo | Qué prueba | Cuándo corre |
|---|---|---|
| **Unitarios** | Validators, prompt_builder, extractor de estado | Cada commit (CI) |
| **Integración** | Orchestrator end-to-end con mocks de LLM | Cada commit (CI) |
| **Golden conversations** | Las 186 conversaciones reales | Manualmente antes de deploy + nocturno |
| **End-to-end** | Webhook completo con sandbox de Evolution | Antes de cada release |
| **Smoke** | Health, readiness, conexión a servicios | Continuamente en producción |

### 13.2 Golden conversations — cómo funcionan

Cada conversación real se guarda como un JSON:

```json
{
  "session_id": "5218441302112@s.whatsapp.net",
  "fecha_original": "2026-05-13",
  "turns": [
    { "role": "user", "content": "Hola buenas tardes" },
    { "role": "assistant_original", "content": "¡Hola! Qué gusto..." },
    { "role": "user", "content": "Busco información de primaria" },
    { "role": "assistant_original", "content": "..." }
  ]
}
```

El runner toma cada turno del usuario, le pide a Sofía 2.0 que responda, y compara la respuesta nueva contra la original con un juez (Claude Sonnet 4.6 con prompt específico):

```
Como juez, evalúa si esta respuesta nueva preserva la intención, tono y dirección del journey de la respuesta original.
Categorías: equivalente | mejor | peor | regresión-crítica.
```

**Objetivo:** ≥85% equivalente o mejor, 0% regresión-crítica.

### 13.3 Bugs específicos que deben tener test

- `test_no_resemana_alianza()`: la frase de alianza no debe aparecer dos veces en una conversación.
- `test_no_pregunta_repetida_nivel()`: si el papá ya dijo el nivel, no se le pregunta de nuevo.
- `test_estado_persiste_tras_salir()`: tras `/salir` del modo aprendizaje, los datos capturados antes siguen ahí.
- `test_envio_imagen_solo_kinder()`: la imagen de costos kinder solo se envía si la pregunta es de kinder.
- `test_no_envio_fantasma()`: la respuesta no menciona "ya te envié X" sin haber llamado al tool.

---

## 14. Observabilidad y costos

### 14.1 Logs estructurados

Cada turno genera un log JSON con:

```json
{
  "ts": "2026-05-18T14:23:11Z",
  "session_id": "...",
  "turn_number": 7,
  "intent": "pregunta_costos_primaria",
  "fase": "informacion",
  "tokens_input": 6234,
  "tokens_output": 187,
  "cache_hit_tokens": 4521,
  "cost_usd": 0.0089,
  "latency_ms": 3421,
  "validators_failed": [],
  "tools_used": ["kb_search"],
  "model": "claude-haiku-4-5"
}
```

Sink: stdout (capturado por Docker / EasyPanel) + tabla `sofia_turn_logs`.

### 14.2 Métricas clave (KPIs operativos)

- **Volumen:** mensajes/día, conversaciones/día, conversaciones únicas/semana.
- **Conversión:** % conversaciones que llegan a fase "agendado".
- **Calidad:** % validators failed, tasa de regeneración.
- **Costos:** $/día, $/conversación, $/agendado.
- **Latencia:** P50, P95, P99 por turno.
- **Errores:** tasa de exceptions, fallos de tool, timeouts.

### 14.3 Alertas

Configurar alertas (email a Oscar):

- Costo diario > $5 (= ~150 conversaciones/día, anómalo).
- P95 latencia > 15s.
- Tasa de error > 1% en 1h.
- 0 mensajes en 6h en horario hábil (algo se rompió).

### 14.4 Dashboard de costos

`/admin/costs` muestra:

- Costo total mes actual vs mes anterior.
- Top 10 conversaciones más caras (debug).
- Breakdown por modelo (Haiku vs GPT-4o-mini vs Whisper).
- Proyección fin de mes.

---

## 15. Roadmap a Fase 2 (resto del ecosistema Maple)

Sofía 2.0 es la primera pieza. La arquitectura está diseñada para que las próximas piezas reusen el mismo stack.

### 15.1 Agente Académico (el que Cecilia más quería)

- **Función:** audita planeaciones docentes contra el modelo educativo Maple, da retroalimentación.
- **Canal:** web app (no WhatsApp) — los docentes suben planeaciones, ven feedback en pantalla.
- **Stack:** mismo FastAPI + Supabase + Claude, pero **modelo Sonnet 4.6 o Opus 4.7** (la tarea es analítica y vale el costo).
- **Reusa:** adapters, observabilidad, validators framework.
- **Construye nuevo:** UI web (Next.js), rúbricas de evaluación, dashboard para Cecilia.

### 15.2 Agente de Comunicación con Familias

- **Función:** analiza mensajes de familias, clasifica tono/urgencia, sugiere respuesta a Lily.
- **Stack:** mismo, con cola de mensajes.
- **No envía automáticamente** — siempre propone, Lily aprueba.

### 15.3 Dashboard Maple

- **Función:** panel central para Cecilia que ve todos los agentes, métricas, KPIs.
- **Stack:** Next.js + Supabase + acceso a las tablas de cada agente.
- **Empieza simple:** vista de conversaciones de Sofía + agendados + alertas.

### 15.4 Orden recomendado de construcción Fase 2

1. Sofía 2.0 estable en producción (~2 meses).
2. Empezar Agente Académico (~2 meses).
3. Dashboard mínimo Maple (~3 semanas).
4. Agente Comunicación Familias (~1.5 meses).
5. Resto.

---

## Apéndice A — Stack de referencia (lo que ya tienes vs lo que vas a usar)

| Pieza | Lo que ya tienes (Sofía actual) | Lo que vas a usar (Sofía 2.0) |
|---|---|---|
| LLM | gpt-5-mini, gpt-5.4-mini, gpt-4.1-mini, gpt-4o-mini ×2 | Claude Haiku 4.5 (principal) + GPT-4o-mini (auxiliar) |
| Orquestación | n8n workflow visual | Python (FastAPI, llamadas directas) |
| Memoria | Postgres `chat_histories_sofia` (n8n) | Postgres `sofia_messages` (propio) |
| Vector store | Supabase pgvector | Mismo |
| KB | 16 chunks, 1 PDF | Mismo de base, se amplía |
| WhatsApp | Evolution API | Mismo |
| Debounce | Redis (Sofía actual) | Mismo patrón |
| Hosting | VPS Hostinger + EasyPanel | Mismo |
| Versionado | UI de n8n | GitHub + PRs |
| Tests | Ninguno | pytest + golden conversations |
| Observabilidad | Logs de n8n | Logs estructurados + dashboard admin |

---

## Apéndice B — Credenciales y accesos que necesitamos confirmar

Antes de empezar Sprint 0, tener:

- [ ] Acceso a Supabase del proyecto `rugchqrjuxjqhdcfohby` (donde vive `documents_maple` y `chat_histories_sofia`).
- [ ] Connection string del Postgres (host, puerto, DB, user, password).
- [ ] API key de OpenAI con presupuesto suficiente.
- [ ] API key de Anthropic (nueva, dedicada a Sofía 2.0 para que el billing sea separado).
- [ ] Credenciales de Evolution API (instancia existente — solo para Sprint 10 en adelante).
- [ ] **Bot de Telegram creado en @BotFather + token guardado** (para Sprint 3.5).
- [ ] **Número WhatsApp dedicado al QA + nueva instancia Evolution** (para Sprint 10).
- [ ] Acceso al Drive "Sistema RAG" (compartido con `ing2oscar@gmail.com`).
- [ ] Acceso SSH al VPS Hostinger.
- [ ] Dominio o subdominio para servir el Web Chat (ej. `sofia.rrintecai.co` o `chat.maplesaltillo.com`).
- [ ] GitHub repo `RR-INTEC/sofia-maple` creado.

---

## Apéndice C — Glosario

- **Agent loop:** ciclo observar → pensar → actuar. Sofía NO es agent loop, es workflow.
- **Augmented LLM:** patrón LLM + tools + memoria + retrieval. Lo que es Sofía.
- **Golden conversation:** conversación real que se usa como test de regresión.
- **Prompt caching:** descuento de Anthropic por reutilizar prefijos de prompt entre llamadas.
- **Session ID:** identificador único de un papá = su número de WhatsApp completo (`<num>@s.whatsapp.net`).
- **Turn:** un par (mensaje del papá + respuesta de Sofía).
- **Modo Aprendizaje:** estado especial activado con `maple2026` donde el equipo Maple da feedback.

---

## Apéndice D — Notas para Claude Code

Cuando arranques con este proyecto, lee este documento completo primero. Después:

1. **No re-litigues las decisiones de la sección 2.** Están tomadas con justificación. Si encuentras una razón fuerte para cambiar una, comunícamela antes de cambiar.

2. **Respeta los principios no negociables de Cecilia** (sección 3.1). Si una feature los viola, no la implementes y avisa.

3. **El copy de la Sofía actual es sagrado.** Lo reorganizas, no lo reescribes. Si necesitas tocar el contenido (más allá de eliminar duplicaciones), avisa primero.

4. **Test antes de declarar listo.** Cada sprint termina con tests pasando, no con "compila".

5. **Costos visibles desde el día 1.** El cálculo de costo por turno debe estar implementado en Sprint 1, no al final.

6. **Migración no destructiva.** En ningún sprint tocamos la Sofía de n8n actual hasta Fase 3 (Sprint 11).

7. **Documenta decisiones que tomes durante la implementación** en `docs/DECISIONS.md`. Cuando hagas un trade-off, regístralo.

8. **Cuando dudes, simplifica.** Es más fácil añadir complejidad que removerla.

---

**Fin del documento.**

*Documento generado el 18 de mayo de 2026 a partir de: brief original de Cecilia Trujillo, auditoría técnica de la Sofía actual de n8n (export del 15 de mayo de 2026), análisis de 186 conversaciones reales de producción, brief de fundamentos de agentes de Oscar Rodríguez, y consenso técnico actualizado sobre agentes de IA en producción.*
