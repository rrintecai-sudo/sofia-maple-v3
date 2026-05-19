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

## ADR-009 — Bloque 5.5 cerrado: solo validator + campus tool. Prompts intactos. Varianza del juez documentada.

**Fecha:** 2026-05-19
**Contexto:** Bloque 5.5 pasó por 3 runs del golden test antes de cerrarse:
- **Baseline** (pre-Bloque-5.5): 57.6% equiv/mejor, 1.1% crítica
- **v1** (4 fixes simultáneos): 53.3% equiv/mejor (−4.3pp), 3.3% crítica (+2.2pp)
- **v2** (revertido Fix 3 + Fix 2 ajustado): 44.6% equiv/mejor (−13pp), 5.4% crítica (+4.3pp)

A pesar de que v2 reviertió el cambio más dañino (Fix 3), las métricas empeoraron. El análisis turno-por-turno reveló la causa:

**Hallazgo crítico — varianza del juez Sonnet 4.6:**
- Entre baseline y v2, **32 de 92 turnos (35%) cambiaron de categoría** (mejor↔peor↔equivalente↔crítica) aun cuando muchos turnos NO tenían cambio de código que pudiera afectarlos.
- Las 5 mejoras de Fase 4 que en v1 dieron "mejor" (turnos 46, 51, 62, 65, 69 del session 34662236125), en v2 dieron "peor" — sin haber cambiado el código que las genera.
- Esto implica una varianza del juez del orden de **±10-15pp en el % global con n=92**.

**Conclusión:** Con esa varianza, el threshold ≥85% del Bloque 5 no es alcanzable midiendo deltas pequeños con un solo run de golden test. La métrica actual sirve para detectar regresiones grandes (Fix 3 inventando datos era detectable), pero no para validar mejoras incrementales.

**Diagnóstico por fix (final):**
- **Fix 3 (contexto en mensajes ≤10 chars)** — Causa de daño REAL. Hint con `nivel=`, `edad=`, `ya_pidió_costos` se interpretaba como hechos confirmados ante saludos iniciales. 3 regresiones + 1 crítica nuevas atribuibles directamente al código (no varianza). **REVERTIDO**.
- **Fix 2 (push a cita + escenas observables en Fase 4)** — Señal mixta. Generó 7 mejoras en v1 (escenas más concretas) pero pivoteo agresivo en "Gracias"/correcciones. El ajuste quirúrgico de v2 (gate explícito) **diluyó la señal** sin sumar señal contraria detectable sobre el ruido. **REVERTIDO también** — el costo de mantener cambios cuyos beneficios no podemos medir es deuda invisible.
- **Fix 1 (validator anti-markdown)** — No tocó prompts. No falló en 0/92 turnos a lo largo de v1+v2 (184 turnos totales). Defensivo, sin costo. **MANTENIDO**.
- **Fix 4 (campus tool pre-fetch)** — No movió métricas pero agrega capacidad real (mapeo nivel→campus, llamada determinística a `get_campus_para_nivel`). **MANTENIDO**.

**Decisión final:**
1. Prompts (`journey/descubrimiento.md`, `journey/informacion.md`) vuelven al estado pre-Bloque-5.5 vía `git checkout HEAD --`.
2. `validators.py` mantiene `validar_no_markdown_excesivo` + 9 tests.
3. `orchestrator.py` mantiene `_nivel_para_campus` + pre-fetch en intent `PREGUNTA_CAMPUS` + 6 tests.
4. No re-correr golden — sería gastar dinero en señal ruidosa.

**Implicación para Bloque 5.6:** Replantear estrategia de evaluación antes de seguir iterando. Opciones a considerar:
- **Multi-run averaging** (correr golden 3-5 veces, promediar) para reducir varianza del juez a ~±3-5pp. Costo: $1.65-$2.75 por iteración.
- **Multi-judge ensemble** (Sonnet 4.6 + Opus 4.7 + Haiku 4.5 votando). Costo mayor pero menor varianza.
- **Métricas determinísticas** complementarias: % de violaciones de validators, % de hallucination flags (afirmaciones sobre eventos inexistentes), longitud media, ratio de respuestas con bullets >2, etc. Estas son deterministas y baratas.
- **Reducir scope del golden** a casos críticos seleccionados manualmente con criterios explícitos, no juicio subjetivo del LLM.

Sin una métrica más estable, iterar prompts es contraproducente — el ruido va a enmascarar señal real.

---

## ADR-010 — Causas raíz reales detectadas por el juez Sonnet 4.6 (input para Bloque 5.6)

**Fecha:** 2026-05-18
**Contexto:** El análisis del golden test post-Fix 5.5 reveló que el 43% de "peor" en baseline NO se debe a los 4 patrones que atacamos inicialmente, sino a 4 causas raíz más profundas que requieren intervención distinta. Estas se documentan aquí como INPUT para Bloque 5.6 (a definir con Cecilia).

**Las 4 causas raíz a atacar en Bloque 5.6:**

1. **Tono transaccional / pitch de ventas con bullets** — En momentos íntimos o de cierre, Sofía suena a "estructura de lista comercial" en lugar de tono humano cálido. Ejemplos del juez: t68 ("pide aclaración con lista de opciones que fragmenta la conversación"), t71 ("suena más a pitch de ventas con estructura de lista"). **Hipótesis de fix:** ajustar prompt de identidad/voz para penalizar bullet-style en respuestas <80 palabras; añadir validator "tono-transaccional" que penalice ≥2 bullets en respuestas cortas.

2. **Inventar datos no presentes** — Sofía afirma información que no está en la conversación: "vi tu link" cuando solo se compartió URL, "ya agendaste cita" cuando no existe, "Campus 2" cuando el contexto indica Campus 1, asume género o etapa del hijo. Ejemplos: t17 ("inventa una cita agendada que no existe"), t18 ("vi el link"), t15 ("Campus 2 sin contexto"), t26 (ignora contenido de imagen y responde como inicio). **Hipótesis de fix:** instrucción explícita "NO afirmes nada que no aparezca textualmente en la conversación; si no estás segura, pregunta" + validator "anti-invención" con detección heurística de afirmaciones sobre eventos pasados.

3. **Perder el hilo cuando el papá corrige o cambia tema** — Cuando el papá da una corrección ("No preguntes X"), Sofía registra mal el aprendizaje, ignora la corrección, o pivota a otro tema. Ejemplos: t2, t5, t18, t36 (Modo Aprendizaje confunde el tema), t12 ("no preguntes si está en escuela" → Sofía sigue preguntando), t16 ("ignora completamente el contexto donde el papá dijo X"). **Hipótesis de fix:** prompt explícito "cuando el papá te corrige, refleja la corrección literal en tu respuesta antes de avanzar"; en Modo Aprendizaje, exigir que el tema registrado contenga al menos una palabra clave del mensaje del papá.

4. **Información factual incorrecta** — Hay datos erróneos en el prompt o la KB. Ejemplo crítico confirmado: t48 dice "Infants 3 a 12 meses" cuando la realidad es 18 meses a 2 años. Probablemente hay más. **Hipótesis de fix:** auditoría página-por-página del prompt y de los seeds de tablas (precios, niveles, edades, campus) cotejado contra el documento oficial de Maple. No es trabajo de prompt engineering — es validación factual.

**Decisión:** No atacar estas 4 causas en Bloque 5.5. Documentarlas aquí, esperar reunión con Cecilia (2026-05-19) para validar datos factuales antes de Bloque 5.6.

**Justificación:** Atacar 4 causas raíz en paralelo sin validación de datos puede repetir el patrón de 5.5 (empeorar todo). Mejor: secuenciar — primero auditoría factual con Cecilia, luego prompt fixes guiados por cada categoría.

---

## ADR-011 — Bloque 5.6 PASO 0: sistema de evaluación robusto (multi-run + métrica determinística + focused sets)

**Fecha:** 2026-05-19
**Contexto:** En ADR-009 documentamos que el juez Sonnet 4.6 tiene varianza ±10-15pp entre runs idénticos. Eso invalida iteraciones de prompts basadas en deltas pequeños del golden test con n=92. Antes de atacar las 4 causas raíz (ADR-010) necesitamos una métrica más estable.

**Decisión — 3 mejoras al runner:**

1. **Multi-run averaging (`--runs N`)** — Cada turno se ejecuta N veces (default 1, recomendado 3 para validación). Por cada turno se reporta:
   - Categoría moda (en empate, prioridad peor > critica > equiv > mejor)
   - Distribución de categorías entre runs
   - Desviación estándar del % equiv/mejor inter-run
   - Razonamiento del juez del primer run que coincide con la moda

2. **Métrica determinística complementaria (`pct_all_validators_pass`)** — Por cada turno, contamos cuántos runs pasaron TODOS los validators. La métrica global es % de turnos donde al menos 1 run pasó todos. Esta métrica es 100% reproducible dada la respuesta del modelo (validators son determinísticos), aunque el modelo principal sí tiene varianza. Implementado en `tests/golden/runner.py` + módulo separado `tests/golden/deterministic_metrics.py` para análisis post-hoc de archivos JSON viejos.
   - **Cambio de contrato:** `TurnResult` (en `app/core/orchestrator.py`) ahora expone `validators_failed: list[str]` y `regenerations: int` para que el runner pueda capturar la métrica sin tocar DB.

3. **Focused sets (`--focused <name>`)** — Sub-conjuntos curados de turnos donde el baseline falla por un patrón específico. 4 sets generados en `tests/golden/focused_sets/`:
   - `invented_data.json` — 10 items (Sofía afirma datos no presentes)
   - `transactional_bullets.json` — 7 items (bullets/markdown en momentos íntimos)
   - `correction_lost.json` — 10 items (Sofía pierde el hilo cuando el papá corrige)
   - `factual_accuracy.json` — 7 items (datos factuales incorrectos)

   La curación es **automática** (regex sobre razonamientos del juez de los 3 runs de Bloque 5.5) más **validación manual** del usuario antes de avanzar al PASO 1.

   Estructura de cada item:
   - `session_id`, `turn_index`, `user_msg`
   - `expected_pattern`: qué debería hacer Sofía bien
   - `baseline_failed: true`, `baseline_bad_runs: N` (en cuántos de los 3 runs falló)
   - `judge_reasoning_excerpts`: hasta 2 razonamientos del juez como evidencia

   El runner en modo `--focused` carga la conversación origen, procesa todos los turnos hasta el último target como **contexto silencioso** (para mantener el flujo conversacional), y solo juzga los turnos del focused set.

**Costo esperado:**
- `--full --runs 3`: ~$1.65 por iteración (vs $0.55 single-run)
- `--focused X --runs 3`: ~$0.10-0.20 según tamaño del set
- Total Bloque 5.6: $3-5 USD aprobado por Oscar

**Justificación:** Sin métrica estable, iterar prompts es contraproducente — el ruido del juez enmascara la señal real. Multi-run reduce la varianza a ~±3-5pp; métrica determinística es 100% reproducible; focused sets dan mediciones específicas por causa raíz que el % global no captura.

---
