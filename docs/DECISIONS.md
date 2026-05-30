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

## ADR-012 — Bloque 5.6 PASO 1: validator `no_inventa_datos` + regla "Integridad de información"

**Fecha:** 2026-05-19
**Contexto:** Causa raíz #1 del baseline (Sofía afirma información que no está en la conversación) es la más frecuente y la más dañina para la confianza del papá. Necesitamos defensa en dos capas: prompt + validator post-hoc.

**Decisión:**

1. **Validator `validar_no_inventa_datos(respuesta, estado, mensajes_papa)`** en `app/core/validators.py` con 7 sub-chequeos:
   - Afirmar haber visto contenido externo (link/imagen/video/post) — siempre falla; Sofía no tiene visión web.
   - Afirmar nombre del papá no presente en `estado.nombre_papa` ni en mensajes previos.
   - Afirmar nivel del hijo (Kinder/Maternal/etc.) no presente en estado ni mensajes.
   - Afirmar edad del hijo no presente.
   - Afirmar género (hijo vs hija) cuando NO hay ningún referente — toleramos si hay nivel_buscado_actual o hijos en estado.
   - Afirmar Campus específico que contradiga `estado.campus_cita`.
   - Afirmar cita agendada cuando `estado.cita_agendada=False`.

   Conservador por diseño: preguntas hipotéticas ("¿para Maternal?") NO fallan. Solo afirmaciones declarativas.

2. **TurnResult expone `validators_failed: list[str]` y `regenerations: int`** (cambio del Bloque 5.6 PASO 0) para que el runner capture la métrica determinística.

3. **Nuevo flujo en orchestrator:** se pasa `mensajes_papa` (extraídos del historial) al `run_all_validators`. Cuando el validator falla, el feedback va al loop de regeneración existente.

4. **Regla "Integridad de información"** en `app/core/prompts/rules.md` (sección nueva, después de regla 22). Cubre:
   - Nombre del papá (con ejemplo concreto del bug detectado: "Gracias por comunicarte con Gaby En digital" → Gaby es sistema, no papá).
   - Género del hijo (usar "tu peque" si no se sabe).
   - Edad/nivel/escuela actual.
   - Eventos pasados (cita agendada).
   - Campus.
   - Contenido externo (URLs/imágenes).
   - Datos cuantitativos específicos (debe venir de tool/tabla, no del prompt).

**Tests:** 15 tests nuevos en `tests/test_validators.py` cubriendo los 7 sub-chequeos (positivos y negativos). 254/254 total.

**Justificación:** Defensa en profundidad: el prompt previene la mayoría de las invenciones; el validator captura las que pasan. La regla del prompt enseña al modelo el principio ("silencio honesto > afirmación cómoda pero falsa"); el validator hace el chequeo determinístico y, al regenerar, inyecta el motivo específico.

---

## ADR-013 — Bloque 5.6 PASO 2: tabla `niveles_por_edad` + tool `niveles.py` + auditoría factual

**Fecha:** 2026-05-19
**Contexto:** Causa raíz #4 — Sofía a veces afirma datos factuales incorrectos. El caso más visible: "Infants 3-12 meses" cuando lo correcto es 18m-2a. Al auditar `app/core/prompts/**/*.md` confirmamos que el dato del prompt está bien; el modelo se equivoca al re-decirlo. El fix: tool determinística que devuelve el dato canónico desde una tabla.

**Decisión:**

1. **Auditoría completa** documentada en `docs/AUDIT_FACTUAL_DATA.md`:
   - Niveles + edades (Cubs Baby 3-11m, Baby 12-18m, Infants 18-24m, Toddlers 24-36m, etc.)
   - Direcciones de Campus 1/2
   - Horarios escolares
   - Costos por nivel
   - Modalidades de estancia
   - **Hallazgo crítico:** el prompt NO especifica capacidades de aula. Sofía las inventaba ("máximo 8 niños", "máximo 20 niños"). La regla "Integridad" del PASO 1 ya prohíbe esto; cuando Cecilia provea los datos reales, se agregan a la tabla.

2. **Migration `004_niveles_por_edad.sql`** crea tabla con:
   - `nivel`, `nombre_display`, `categoria` (maternal/kinder/primaria/secundaria)
   - `edad_min_meses`, `edad_max_meses` (en meses para precisión cross-boundary)
   - `grados[]`, `descripcion`, `campus`
   - `vigente BOOL DEFAULT TRUE`
   - **`confirmado_por_cliente BOOL DEFAULT FALSE`** — flag para que Cecilia valide
   - `fuente TEXT` — origen del dato ('prompt_v2.8' inicialmente)

   Seed con 8 niveles desde el prompt v2.8. La tool consulta `vigente=TRUE` indistintamente del flag (pending validación). **Aplicada en Supabase prod el 2026-05-19** vía Management API.

3. **Tool `app/tools/niveles.py`** con 3 funciones:
   - `consultar_nivel_por_edad(edad_meses)` — qué nivel cubre N meses
   - `consultar_edades_de_nivel(nivel)` — qué edades cubre un nivel
   - `listar_niveles_vigentes()` — todos los niveles ordenados

   El dataclass `NivelInfo` tiene un helper `rango_legible()` que devuelve strings correctos cross-boundary ("18 meses a 2 años") — anti-bug del caso Infants.

4. **Wiring en orchestrator:** nuevo helper `_detectar_nivel_en_mensaje(mensaje)` busca keywords (infants/baby/cubs/toddlers/preschool/maternal/kinder) y, si encuentra, hace pre-fetch via `consultar_edades_de_nivel`. El resultado se inyecta como hint al prompt junto con campus (ya existente).

5. **NO se modifican los prompts existentes** — los datos del prompt son correctos según el audit. La defensa contra invenciones del modelo viene de:
   - Regla "Integridad de información" del PASO 1 (rules.md)
   - Validator `no_inventa_datos` del PASO 1
   - Tool determinística `niveles.py` (este paso)

**Deuda para Bloque 5.7 (post-validación con Cecilia):**
- Validar tabla `niveles_por_edad` → marcar `confirmado_por_cliente=TRUE`
- Capturar capacidades de aula (ratio niños:educadora) y agregar a tabla nueva `capacidades_aula`
- Considerar mover precios/horarios del prompt al esquema de DB y dejar el prompt apuntando a las tools

**Tests:** 16 nuevos (10 de tool + 6 del helper en orchestrator). 270/270 total.

---

## ADR-014 — Bloque 5.6 PASO 3: intimacy_detector + validator no_bullets_intimo + regla tono íntimo

**Fecha:** 2026-05-19
**Contexto:** Causa raíz #2 — Sofía responde con bullets/listas/markdown en momentos íntimos (descubrimiento emocional, "Sí" tras pregunta vulnerable). El juez Sonnet 4.6 lo describe como "pitch de ventas con estructura de lista" o "folleto comercial" — rompe la calidez que distingue a Sofía vs venta tradicional.

**Decisión:** Defensa en 3 capas:

1. **`app/core/intimacy_detector.py`** — heurística determinística (sin LLM por default) que clasifica el mensaje del papá en momento_intimo vs no_intimo. Reglas (en orden):
   - Keywords emocionales o narrativa personal → íntimo, conf 0.9
   - Patrón operativo (precios/horarios/citas) sin señales emocionales → no íntimo, conf 0.95
   - Short followup ("sí", "ok", "qué más") tras mensaje emocional previo → íntimo, conf 0.7
   - Short followup en fase descubrimiento sin contexto → íntimo, conf 0.55
   - Mensaje sustancioso (>40 chars) en descubrimiento → íntimo, conf 0.7
   - Default → no íntimo, conf 0.4

   Wrapper `detectar_intimidad_async()` con opción de fallback a GPT-4o-mini cuando `confianza < threshold`. Fallback graceful: si OpenAI no responde, devuelve el resultado heurístico (NO bloquea el flujo principal — cumple regla operativa #4 de Oscar).

   **Decisión técnica clave:** keywords son frases compuestas ("le cuesta", "me cuesta") en vez de palabras sueltas, porque "cuesta" sola matchearía "¿cuánto cuesta?" (operativo). Orden de reglas: emocional > operativo, porque palabras como "cuando" aparecen en frases emocionales ("cuando yo era niño") y también en operativas ("cuándo abren"); las emocionales son menos comunes y específicas.

2. **Validator `validar_no_bullets_en_momento_intimo(respuesta, es_momento_intimo)`** — más estricto que `validar_no_markdown_excesivo`:
   - Si `es_momento_intimo=False` → siempre pasa (no aplica)
   - Si `es_momento_intimo=True` → falla con ≥2 bullets, ≥2 numerados, o ≥3 negritas
   - Feedback al regenerar pide reescribir en prosa fluida, 2-4 oraciones máximo.

3. **Regla "TONO EN MOMENTOS ÍNTIMOS"** en `app/core/prompts/journey/descubrimiento.md` (después de "Durante el descubrimiento"). Con ejemplos MAL/BIEN. Define cuándo aplica (descubrimiento + mensajes cortos de seguimiento) y cuándo no (operativo).

**Wiring en orchestrator:**
- Después de `extraccion + intent_task`, llamamos `detectar_intimidad(mensaje, estado)` sync. Es heurística pura (regex/keywords), no necesita `asyncio.gather`. Fallback graceful via try/except.
- Resultado se pasa a `run_all_validators(es_momento_intimo=...)` y se inyecta como hint al `mensaje_para_llm` cuando es íntimo: `"[Hint interno: este es un MOMENTO ÍNTIMO. Responde en prosa fluida...]"`.

**Tests:** 14 del detector + 6 del validator = 20 nuevos. 290/290 total.

---

## ADR-015 — Bloque 5.6 PASO 4: intent CORRECCION_DEL_PAPA + correction_handler

**Fecha:** 2026-05-19
**Contexto:** Causa raíz #3 — Sofía pierde el hilo cuando el papá corrige o aclara algo. En golden tests: el papá dice "no preguntes si está en otra escuela actualmente" y Sofía la siguiente vuelve a preguntar; el papá dice "no, es Kinder no Maternal" y Sofía sigue hablando de Maternal.

**Decisión:**

1. **Nuevo `Intent.CORRECCION_DEL_PAPA`** en `app/core/intent_classifier.py`. Detecta:
   - Negaciones directas: *"no, eso no era"*, *"no me refería a eso"*
   - Aclaraciones: *"te corrijo"*, *"déjame aclarar"*, *"estás confundido/a"*, *"mira, lo que pasa es que..."*
   - Instrucciones procedimentales: *"no preguntes X"*, *"cuando te diga Y, haz Z"*

2. **Nuevo módulo `app/core/correction_handler.py`**:
   - `CorreccionDetectada` (dataclass) con campos: `nivel_buscado`, `nombre_hijo`, `edad_hijo`, `grado_hijo`, `escuela_actual`, `nombre_papa`, `instruccion_comportamiento`, `campos_a_limpiar: list[str]`.
   - `async detectar_correccion(mensaje, estado)` — llama a GPT-4o-mini con un prompt específico para identificar QUÉ se está corrigiendo. Devuelve `None` si LLM falla (graceful) o `CorreccionDetectada` (puede ser vacía).
   - `aplicar_correccion(estado, correccion)` — devuelve EstadoCapturado nuevo con los campos sobreescritos. A diferencia del extractor regular, **sí pisa** valores previos (el papá nos dice que estaban mal).

3. **Wiring en orchestrator (paso 5bis):**
   - Solo dispara cuando `intent == CORRECCION_DEL_PAPA`.
   - Try/except envuelve la llamada — fallback graceful: si falla, el orchestrator continúa sin aplicar corrección.
   - Si la corrección detectada NO es vacía, se aplica a `estado.estado_capturado`.
   - Hint inyectado al `mensaje_para_llm` con resumen de la corrección + instrucción explícita: "Reconoce humildemente, confirma el dato correcto, continúa desde ahí".

4. **Regla "Cuando el papá te corrige"** en `app/core/prompts/rules.md` (antes de la sección "Integridad de información"). Cuatro pasos: (1) disculpa breve sin exceso, (2) confirmar dato correcto, (3) continuar journey desde el dato actualizado, (4) NO repetir dato viejo. Cubre también correcciones procedimentales.

**Costo extra:** una llamada GPT-4o-mini adicional cuando el papá corrige. Estimado: ~$0.001 por corrección. Despreciable.

**Tests:** 15 nuevos en `tests/test_correction_handler.py` (4 dataclass, 7 aplicar_correccion, 4 detectar_correccion con monkeypatch). 305/305 total.

**Decisión técnica clave — graceful fallback:** las 3 piezas (intent_classifier, correction_handler, orchestrator) están encadenadas por if/except. Si cualquiera falla, el flujo continúa con `correccion=None`. Cumple regla operativa de Oscar: "Fallback graceful si fallan. No bloquea el flujo principal."

---

## ADR-016 — Bloque 5.6 ejecutado, NO PASA criterios. Resultado documentado en `tests/golden/results/bloque-5.6-final.md`

**Fecha:** 2026-05-19
**Contexto:** Tras ejecutar los 5 focused sets + full --runs 3 parcial (73/92 turnos), Bloque 5.6 NO cumplió los criterios del spec (1.5 / 5).

**Resultados (resumen):**
- invented_data: 20% equiv/mejor (criterio >50% — falla)
- factual_accuracy: 50% (operativo — pasa parcial)
- transactional_bullets: 25% (criterio >30% — falla)
- correction_lost: 0% + 33% crítica (criterio >30% — falla, regresión)
- learning_mode_failures: 0% (no atacado, info-only)
- full parcial (73/92): 49.3% equiv/mejor vs 61.6% baseline en mismos turnos (−12.3pp), 0% crítica en ambos
- Costo total: ~$2.52 USD

**Aprendizajes documentados:**
1. Los validators anti-invención hacen Sofía más cautelosa pero más vacía. El juez Sonnet 4.6 prefiere v1 generosa-aunque-imprecisa sobre v2 cuidadosa-pero-vacía.
2. `intimacy_detector` heurístico tiene falsos negativos en short messages ("5 to", "Sí") que sí deberían ser íntimos.
3. Intent `CORRECCION_DEL_PAPA` no captura "Si"/"Si por favor" porque técnicamente son confirmaciones, no correcciones. El bug real de `correction_lost` en el baseline es "respuesta corta con pérdida de contexto", no "papá corrige" — requiere otro intent/handler.
4. `pct_all_validators_pass` es 100% en todos los runs, pero NO correlaciona con la métrica del juez. Los validators son demasiado permisivos para medir calidad conversacional.

**Lo único que claramente mejoró:** 0 regresiones críticas en el full (vs 1.1% baseline). Pero introdujo críticas nuevas en focused isolation (correction_lost: t11, t70) que NO aparecen en el flujo continuo del full.

---

## ADR-017 — Bloque 5.6 cerrado parcialmente: revert prompts/validators, mantener infraestructura

**Fecha:** 2026-05-19
**Contexto:** Resultado del Bloque 5.6 (ADR-016): NO pasa criterios. Oscar decidió **Opción A** de las tres recomendadas: revertir prompts/validators del 5.6 manteniendo la infraestructura útil. Esto **NO es una pausa** — es limpieza para que Bloque 5.7 (cuando Oscar lo arranque) parta de una base clara, no sobre código que sabemos que empeoró las métricas.

**Decisión — qué se revierte:**

1. **`app/core/validators.py`** — eliminados:
   - `validar_no_inventa_datos` y todos los regex `_AFIRMA_*` asociados (7 sub-checks de invención).
   - `validar_no_bullets_en_momento_intimo`.
   - `run_all_validators` vuelve a 5 validators (era 7).
   - Firma de `run_all_validators` vuelve a la previa (sin `mensajes_papa`, sin `es_momento_intimo`).

2. **`app/core/intent_classifier.py`** — eliminado:
   - `Intent.CORRECCION_DEL_PAPA` del enum.
   - Su descripción en el system prompt.

3. **`app/core/orchestrator.py`** — eliminados:
   - Imports de `correction_handler` e `intimacy_detector`.
   - Handler de corrección (paso 5bis del PASO 4).
   - Detección de intimidad (paso 5pre del PASO 3).
   - Hint inyection de corrección + intimidad al `mensaje_para_llm`.
   - `mensajes_papa` y `es_momento_intimo` de la llamada a `run_all_validators`.
   - **Conservado:** import de `consultar_edades_de_nivel`, helper `_detectar_nivel_en_mensaje`, pre-fetch de niveles tool (paso 5ter), import y pre-fetch de campus (Bloque 5.5 Fix 4).

4. **`app/core/prompts/rules.md`** — eliminadas:
   - Regla "Cuando el papá te corrige".
   - Regla "Integridad de información".

5. **`app/core/prompts/journey/descubrimiento.md`** — eliminada:
   - Sección "TONO EN MOMENTOS ÍNTIMOS".

6. **Archivos completos eliminados:**
   - `app/core/intimacy_detector.py`
   - `app/core/correction_handler.py`
   - `tests/test_intimacy_detector.py`
   - `tests/test_correction_handler.py`

7. **`tests/test_validators.py`** — eliminadas:
   - Secciones de tests para `validar_no_inventa_datos` (15 tests).
   - Secciones de tests para `validar_no_bullets_en_momento_intimo` (6 tests).
   - `test_run_all_returns_7_results` → `test_run_all_returns_5_results`.
   - Asserts de `len(passed) == 7` → `== 5`.

8. **`app/config.py`** — revertido:
   - `max_regenerations_per_turn: int = Field(default=1, ...)` → `default=2` (valor previo al PASO 5.0 del 5.6).

**Decisión — qué se mantiene (infraestructura útil):**

- ✅ `tests/golden/runner.py` con `--runs N` y `--focused <set>` (PASO 0).
- ✅ `tests/golden/deterministic_metrics.py`.
- ✅ `tests/golden/focused_sets/` (5 archivos curados).
- ✅ `tests/golden/results/focused-*.json` (evidencia ejecutada).
- ✅ `migrations/004_niveles_por_edad.sql` y la tabla aplicada en Supabase prod.
- ✅ `app/tools/niveles.py` (tool determinística).
- ✅ `app/core/orchestrator.py` wiring de niveles tool en orchestrator (paso 5ter).
- ✅ `docs/AUDIT_FACTUAL_DATA.md`.
- ✅ ADRs 011-016 intactos.

**Verificación post-revert:**
- Tests: 255/255 ✅ (vs 306 antes — 51 tests eliminados de los módulos revertidos)
- `uv run python -m tests.golden.runner --help` ✅ soporta `--runs` y `--focused`
- `app.tools.niveles` imports OK ✅
- ruff check + format limpios ✅

**Plan tentativo para Bloque 5.7 (cuando Oscar lo arranque):**

Atacar las dos causas raíz reales no resueltas, con base en aprendizajes del 5.6:

1. **"Respuesta corta con pérdida de contexto"** (real bug detrás del fail de correction_lost). El intent debería detectar afirmaciones cortas ("Si", "Ok") cuando el contexto previo establecía un compromiso. Handler: validar que la respuesta del LLM ataque el compromiso pendiente, no salte de tema.
2. **`intimacy_detector` con threshold más sensible** (o con LLM fallback más agresivo para mensajes cortos en descubrimiento).
3. **Considerar `severity=warning` en validators heurísticos nuevos** — registran fallos pero NO disparan regeneración. Mantiene métrica determinística sin costo extra.
4. **Validar tabla `niveles_por_edad` con Cecilia** ANTES de tocar más prompts. Si los datos seed están mal, cualquier mejora de prompt va a pelear contra datos malos.

**Justificación de revertir:** Mantener código que sabemos que empeora métricas crea deuda invisible. Cualquier iteración futura debe partir de un baseline limpio para poder atribuir mejoras o regresiones a cambios concretos.

---

## ADR-018 — Bloque 5.7 ATAQUE 1: validators heurísticos con severity=warning

**Fecha:** 2026-05-19
**Contexto:** La lección clave del Bloque 5.6 fue que los validators heurísticos (anti-invención, anti-bullets en momentos íntimos) con `severity=error` forzaban regeneraciones que hacían a Sofía cautelosa-pero-vacía. El juez prefirió v1 generosa-aunque-imprecisa. La fix: **detectar sin bloquear** — los heurísticos registran señal, no forzan corrección.

**Decisión:**

1. **`ValidationResult.severity`** ya existe con default `"error"` (sobrevivió al revert del 5.6). Los nuevos validators del 5.7 usan `severity="warning"`.

2. **`ValidationReport.all_passed`** filtra por `severity == "error"`: los warnings NO disparan `feedback_para_regenerar()` ni cuentan como falla del flujo.

3. **`validar_no_inventa_datos`** (severity=warning) re-introducido con los 7 sub-chequeos del 5.6 + tolerancia para saludo inicial puro. Mismos regex `_AFIRMA_*`.

4. **`validar_no_bullets_en_descubrimiento(respuesta, fase_journey)`** (severity=warning) — nueva firma simple: si `fase_journey == DESCUBRIMIENTO` Y respuesta tiene ≥3 bullets / ≥3 numerados / ≥4 negritas → warning. NO requiere `intimacy_detector`.

5. **Thresholds calibrados del 5.6 PASO 5.0** (≥3/≥3/≥4) mantenidos — ya están validados como balance entre falsos positivos y verdaderos positivos.

6. **`run_all_validators` firma extendida:**
   - `mensajes_papa` (opcional) → para `no_inventa_datos`
   - `fase_journey` (opcional) → para `no_bullets_descubrimiento`. Si no se pasa, ese validator se omite.

7. **Persistencia (decisión C de la negociación):**
   - `validators_passed_map` y `validators_failed_map`: SOLO errors → persisten en `sofia_turn_logs` (sin cambio de schema).
   - `validators_warnings_map`: nueva property → SOLO se loggea con `log.warning(...)` y se expone via `TurnResult.validators_warnings: list[str]`. NO se persiste en DB.

8. **Orchestrator:** captura warnings tras `run_all_validators`, los loggea con `log.warning("validator_warnings", extra={...})`, y los expone en `TurnResult.validators_warnings`.

9. **Runner (`tests/golden/runner.py`):**
   - `TurnComparison.new_validators_warnings: list[str]` y `any_run_had_warnings: bool`.
   - Nueva función `_warnings_stats()` que calcula % turnos con warnings + histograma por validator.
   - `RunSummary.pct_turns_with_warnings` y `RunSummary.warnings_by_validator`.
   - Output del runner incluye línea "⚠ validator_name: N turnos" + métrica global "% turnos con warnings".
   - Cada turno con warning lleva sufijo `⚠valname` en su línea de output.

**Tests:** 20 nuevos validators tests + 3 tests que confirman que warnings no disparan regen. Total: 274/274 pasando.

**Justificación:** Esta arquitectura permite **medir sin interferir**. Los warnings son señal para iterar (si `no_inventa_datos` warns en >20% turnos, hay patrón sistemático que vale atacar con prompt en futuro bloque). Pero la respuesta del LLM NO se altera por warnings, así que Sofía no se vuelve cautelosa-pero-vacía.

---

## ADR-019 — Bloque 5.7 ATAQUE 2: intent RESPUESTA_CORTA_AL_TURNO_PREVIO + handler de contexto

**Fecha:** 2026-05-19
**Contexto:** El análisis del fail de `correction_lost` en el Bloque 5.6 reveló que el bug real NO era "papá corrige" sino "papá manda mensaje corto + Sofía pierde contexto del último turno propio". Ejemplo: Sofía pregunta "¿En qué grado está tu hijo?", papá responde "5to", Sofía contesta con presentación extensa sobre Tercero de Secundaria.

**Decisión:** Detectar el patrón y forzar a Sofía a tratar el mensaje como respuesta al turno previo.

1. **Nuevo `Intent.RESPUESTA_CORTA_AL_TURNO_PREVIO`** en `app/core/intent_classifier.py` con descripción en el system prompt del LLM. El LLM clasificará algunos casos; el resto los captura el guard heurístico.

2. **Helper determinístico `es_respuesta_corta_al_turno_previo(mensaje, hay_turno_previo_assistant)`** — regex sobre keywords confirmatorias/numéricas/continuación con guard A:
   - ≤15 chars después de trim
   - Match con un patrón confirmatorio/numérico (sí, ok, listo, claro, 5to, 9 años, primaria, kinder, que más, cuéntame, ajá, etc.)
   - **MUST** `hay_turno_previo_assistant=True` (sin turno previo no aplica)

3. **Override en orchestrator (paso 7bis):**
   - Tras leer historial, escanea hacia atrás el último mensaje del assistant.
   - Si la heurística matchea (con guard de turno previo) Y el LLM marcó otro intent → **override**: `intent_result = IntentResult(RESPUESTA_CORTA_AL_TURNO_PREVIO, 1.0, "override heurístico")` con log.
   - Esto cubre el caso donde el LLM se confunde (ej. clasifica "5to" como `confuso_otro`).

4. **Hint inyectado al `mensaje_para_llm` (paso 7ter):**
   ```
   [CONTEXTO CRÍTICO: el papá acaba de responder con un mensaje muy corto
   ({mensaje}). Es una continuación al turno PREVIO tuyo donde dijiste:
   "{ultimo_assistant[:300]}".
   Tu respuesta DEBE:
   1) tratar el mensaje como respuesta a TU pregunta/afirmación.
   2) NO recitar info no pedida.
   3) Si cierra un loop, avanza el journey 1 paso pequeño.
   4) Si es ambiguo, pregunta UNA cosa breve.]
   ```

5. **Nuevo validator `validar_no_recita_info_no_pedida(respuesta, intent)`** con `severity="warning"`. Activa solo cuando intent == RESPUESTA_CORTA_AL_TURNO_PREVIO. Falla (warning) si:
   - Respuesta > 80 palabras
   - Headers (#) en respuesta
   - ≥2 listas numeradas (1. 2.)

**Tests:** 20 tests para el helper heurístico (15 positivos + 5 negativos cubriendo el guard de turno previo, longitud, palabras random) + 5 tests del nuevo validator. Total 299/299 pasando.

**Justificación:** Filosofía 5.7: detectar bug real con heurística + dar contexto explícito en el prompt + medir (no bloquear) con warning. La heurística determinística override garantiza que el patrón se capture incluso cuando el LLM clasifica mal. El validator de soporte mide si Sofía hace caso al hint o lo ignora.

---

## ADR-020 — FIX 4: nombre del papá inventado sube a severity=error (separado del bloque warning)

**Fecha:** 2026-05-29
**Contexto:** Primera prueba REAL por WhatsApp (papá humano, sofia2-test, 2026-05-29). Sofía llamó al papá **"María"** sin que él diera su nombre. Diagnóstico: NO era el `pushName` del JID (el adapter de Evolution nunca lo lee) — fue una **alucinación del LLM principal**. El sub-check de nombre inventado YA existía dentro de `validar_no_inventa_datos`, pero con `severity="warning"` (degradado en ADR-017/ADR-018 por causar regeneraciones excesivas y respuestas "cautelosas-pero-vacías"). Es decir: **lo detectaba y lo dejaba pasar.**

**Decisión:** Separar SOLO el sub-check de nombre a un validator propio `validar_no_inventa_nombre_papa` con `severity="error"` (sí regenera). El resto de `validar_no_inventa_datos` (vio-contenido, nivel, edad, género, campus, cita) **permanece en warning** — NO revertimos ADR-017 en bloque.

**Por qué solo el nombre a error:**
- Inventar el nombre del papá rompe la confianza al instante (es de los peores fallos visibles).
- El sub-check de nombre es de baja ambigüedad (saludo vocativo + nombre propio capitalizado), así que el riesgo de falso positivo es acotable — a diferencia de los de nivel/edad/género que dependen de contexto difuso.

**Mitigación de falsos positivos (la preocupación central de ADR-017):**
- Regex estricta `_NOMBRE_VOCATIVO_RE`: saludo vocativo conocido + palabra capitalizada.
- Denylist `_PALABRAS_NO_NOMBRE` (incluye "maple", "claro", "gracias", "perfecto", etc.) → "Claro, Maple ofrece…" NO se bloquea.
- Exclusiones: nombre en `estado.nombre_papa` o en los mensajes del papá → no bloquea.

**Tests:** 6 unit (incluye 2 anti-falso-positivo: marca "Maple", "Gracias por…") + 2 E2E (terco bloquea / nombre real no bloquea).

**Qué se mantiene en warning:** los 6 sub-chequeos restantes de `validar_no_inventa_datos`. Qué sube a error: solo nombre del papá.

---

## ADR-021 — FIX 1+2+3: el flujo de agendado se desacopla del intent + gate duro anti-confirmación-fantasma

**Fecha:** 2026-05-29
**Contexto:** En la misma prueba real, Sofía falló 3 garantías que dábamos por cerradas (D.2 fecha, D.3 seis-datos, D.4 Maps):
- Dijo "lunes 2 de junio" cuando el lunes era 1; "mañana viernes 30 de mayo" cuando el 30 era sábado.
- No pidió los 6 datos; cerró el agendado con solo nombre+edad del hijo.
- No envió Maps; prometió "Lily te comparte la dirección" sin haber registrado nada.

**Causa raíz común:** todo el andamiaje determinístico (resolver de fecha, gate de 6 datos, override de Maps) estaba **acoplado a `intent == QUIERE_AGENDAR`**, que se evalúa por mensaje individual. En conversación fragmentada ("Mejor lunes", "Mañana", "a las 9") el classifier NO marca QUIERE_AGENDAR, así que `handle_appointment_intent` no corría y el LLM improvisaba fecha + confirmación. Los tests de D.x eran sintéticos: entraban al flujo por la puerta de atrás (intent forzado + datos completos), nunca por conversación real.

**Decisión (3 partes):**
1. **Desacoplar del intent (FIX 1+3):** el orchestrator dispara `handle_appointment_intent` cuando `intent == QUIERE_AGENDAR` **o** `contiene_expresion_temporal(mensaje)` (nuevo detector determinístico en `appointment_extractor`). Así el resolver de fecha y el gate de 6 datos corren en cualquier turno con día/hora.
2. **Fecha resuelta al LLM (FIX 1):** cuando el papá da día sin hora, el handler le pasa la fecha YA RESUELTA ("lunes 1 de junio") y le prohíbe recalcularla. Además, el prompt (`_meta_block`) incluye una **tabla pre-calculada de los próximos 7 días** (día → fecha) como red de respaldo para que Haiku nunca haga aritmética de calendario.
3. **Gate duro anti-confirmación-fantasma (FIX 2+3):** nuevo validator `validar_no_confirma_cita_inexistente` con `severity="error"`. Si la respuesta AFIRMA haber registrado/agendado/confirmado una cita ("registré tu solicitud", "te agendo para", "Lily te comparte la dirección") **y** no existe `appointment_id` real (`cita_realmente_registrada=False`), regenera. Calibrado oración-por-oración para NO bloquear mensajes de proceso/condicionales ("en cuanto me confirmes los datos, registro tu solicitud").

**Por qué un validator de error y no solo el hint:** D.3 ya pasaba un hint pidiendo los datos, pero el hint es una sugerencia que el LLM puede ignorar (y lo ignoró en la prueba real). El gate de error es la red dura: aunque el LLM insista, no sale al papá una confirmación falsa.

**Riesgo asumido:** disparar el flujo ante cualquier expresión temporal añade una llamada a gpt-4o-mini (extractor) en turnos que mencionan un día sin querer agendar. Es acotado y el detector es específico (día/hora explícitos). Aceptado a cambio de fechas correctas y cero confirmaciones fantasma.

**Tests:** 8 E2E de conversación fragmentada (`tests/e2e/test_conversacion_fragmentada.py`) + 6 unit del validator de cita (incluye 3 anti-falso-positivo) + detector temporal cubierto en E2E (routing positivo y control negativo).

**Pendiente de validación REAL:** Oscar repite la conversación de "María" por WhatsApp en sofia2-test antes de cerrar el bloque. Los tests son sintéticos (LLM mockeado); confirman las garantías del orchestrator, no el comportamiento del LLM real.

---
