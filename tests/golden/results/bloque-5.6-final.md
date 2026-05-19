# Bloque 5.6 — Reporte final de evaluación

**Fecha:** 2026-05-19
**Resultado:** ❌ NO PASA criterios del spec del usuario. Bloque cerrado para análisis con Cecilia antes de Bloque 5.7.

---

## Criterios de paso (definidos antes de empezar)

| # | Criterio | Resultado | Pasa |
|---|---|---|---|
| 1 | PASO 0: sistema implementado y operativo | ✅ multi-run + deterministic + 5 focused sets curados y validados | ✅ |
| 2 | PASO 1: `invented_data` mejora >50% | ❌ **20% equiv/mejor** (1/5) | ❌ |
| 3 | PASO 2: 0 datos factuales hardcoded + tabla operativa | ✅ tabla `niveles_por_edad` aplicada en prod, tool funcional, audit completo | ✅ parcial (datos quedan en prompt como contexto, tool agrega defensa) |
| 4 | PASO 3: `transactional_bullets` >30% | ❌ **25% equiv/mejor** (1/4) | ❌ |
| 5 | PASO 4: `correction_lost` >30% | ❌ **0% equiv/mejor + 33% crítica** | ❌❌ |

**Criterios cumplidos: 1.5 / 5.** No alcanzamos los umbrales en 3 de 4 causas raíz.

---

## Métricas globales del full --runs 3 (parcial, 73/92 turnos)

> El full crasheó con `anthropic.BadRequestError: credit balance is too low` después de procesar la primera conversación completa (sid 34662236, 73 turnos). La segunda conversación (sid 5218441302, 19 turnos) NO se ejecutó.

| Métrica | Baseline pre-5.5 (mismos 73 turnos) | Bloque 5.6 (--runs 3) | Δ |
|---|---|---|---|
| **% equivalente o mejor** | **61.6%** | **49.3%** | **−12.3pp** ❌ |
| **% regresión crítica** | 0% | **0%** | igual ✅ |
| Equivalente | 19 | 15 | −4 |
| Mejor | 26 | 21 | −5 |
| Peor | 28 | 37 | +9 |
| Crítica | 0 | 0 | igual |
| % all-validators-pass | n/a | 100% | (nueva métrica) |

### Movimientos turno-a-turno (73 turnos)

| Movimiento | Turnos |
|---|---|
| **iguales** (sin cambio de categoría) | 48 (66%) |
| mejor → peor | 12 |
| peor → mejor | 7 |
| equiv → peor | 5 |
| peor → equiv | 1 |

Diff neto: **−9 turnos**. Consistente con la pérdida de 12pp en el % equiv/mejor.

---

## Métricas por focused set (5 sets, todos con `--runs 3`)

| Set | N | Equivalente | Mejor | Peor | Crítica | % equiv/mejor | Costo |
|---|---|---|---|---|---|---|---|
| invented_data | 5 | 0 | 1 | 4 | 0 | **20%** | $0.10 |
| factual_accuracy | 6 | 0 | 3 | 3 | 0 | **50%** ← mejor del bloque | $0.14 |
| transactional_bullets | 4 | 0 | 1 | 3 | 0 | **25%** | $0.15 |
| correction_lost | 6 | 0 | 0 | 4 | 2 | **0% (+33% crítica)** ← regresión | $0.14 |
| learning_mode_failures | 6 | 0 | 0 | 6 | 0 | **0%** ← no atacamos este bug | $0.29 |

### Lectura por causa raíz

**Causa #1 — Invención de datos (PASO 1):**
20% equiv/mejor sobre 5 turnos donde el baseline fallaba 100% por construcción. Subimos 1 turno a "mejor" (t39 — sistema "Gaby En digital", Sofía mantuvo identidad correcta). Los otros 4 siguen peor pero por **vagueness/frialdad**, no por inventar (el validator pasa 100%). El fix elimina invenciones pero deja la respuesta más vacía.

**Causa #2 — Bullets en momentos íntimos (PASO 3):**
25% equiv/mejor sobre 4 turnos. t65 sigue siendo "excesivamente larga, recargada de bullets" porque el intimacy_detector no lo marcó como íntimo (es "5 to", un short message; está bajo umbral). El detector heurístico tiene falsos negativos.

**Causa #3 — Pérdida de hilo en correcciones (PASO 4):**
**0% equiv/mejor + 33% crítica**. Es el peor resultado del bloque. El intent `CORRECCION_DEL_PAPA` no dispara para mensajes tipo "Si" o "Si por favor" porque técnicamente no son correcciones — son confirmaciones. El handler no aplica y la respuesta termina compartiendo costos sin que el papá los pida (crítica).

**Causa #4 — Datos factuales incorrectos (PASO 2):**
**50% equiv/mejor** — el mejor resultado. Tabla `niveles_por_edad` + tool determinística + pre-fetch en el orchestrator. Aún hay 3 fallos: t15 (Campus 2 sin contexto), t67 (afirmaciones grandilocuentes), t46 (Baby). El campus tool y niveles_por_edad ayudan parcialmente.

**Bonus #5 — learning_mode_failures (6 turnos):**
**0% equiv/mejor**. Era esperado: no atacamos este bug en el Bloque 5.6. La regresión en Modo Aprendizaje desde Sofía v1 es real y separada.

---

## Costos

| Item | Costo |
|---|---|
| Run abortado (validators saturados) | ~$0.40 |
| invented_data (3 runs × 5 turnos) | $0.10 |
| factual_accuracy (3 × 6) | $0.14 |
| transactional_bullets (3 × 4) | $0.15 |
| correction_lost (3 × 6) | $0.14 |
| learning_mode_failures (3 × 6) | $0.29 |
| Full --runs 3 (parcial, 73 turnos) | ~$1.30 |
| **Total Bloque 5.6** | **~$2.52** |

Dentro del presupuesto aprobado ($3-5). La cuenta Anthropic se quedó sin créditos durante el full → cancela parte del full pero no afecta la conclusión: los focused sets ya mostraban el patrón.

---

## Conclusiones

1. **El Bloque 5.6 empeora a Sofía vs baseline pre-5.5 en el flujo continuo (−12pp).** Las defensas anti-invención y anti-bullets están haciendo Sofía más cautelosa pero más vacía: el juez Sonnet 4.6 prefiere la Sofía v1 generosa-aunque-imprecisa sobre la Sofía 2.0 cuidadosa-pero-vacía.

2. **Lo único que claramente mejoró: 0 regresiones críticas en el full.** La regla "Integridad de información" + validador `no_inventa_datos` + tool `niveles_por_edad` eliminaron las invenciones críticas que el baseline tenía (1.1% crítica → 0%). Pero introdujeron críticas NUEVAS en focused isolation (correction_lost) que no aparecen en el flujo continuo del full.

3. **El PASO 0 (sistema de evaluación) fue un éxito.** Multi-run reveló la varianza del juez (±0–20pp por focused) y los focused sets permiten medir cada causa raíz independientemente. **Ese trabajo debe quedarse independientemente del resultado del bloque.**

4. **La métrica determinística `pct_all_validators_pass` es 100%** en todos los focused sets y el full. Pero esto NO predice la métrica del juez — los validators son demasiado permisivos para correlacionar con calidad conversacional. **Necesita validators más finos o métricas más cercanas a "tono cálido"**.

5. **El intimacy_detector heurístico tiene falsos negativos.** Mensajes cortos como "5 to" o "Sí" en fase descubrimiento no se marcan como íntimos pero la respuesta de Sofía sí debería serlo. Requiere reglas adicionales o LLM fallback más agresivo.

6. **El intent CORRECCION_DEL_PAPA solo dispara con negaciones/correcciones explícitas.** Pero el bug real del baseline en `correction_lost` es más amplio: Sofía pierde contexto en respuestas cortas afirmativas ("Si", "Si por favor", "Ok"). Esos casos no son correcciones técnicamente — son confirmaciones. **Necesita otro intent o handler.**

---

## Recomendaciones para decidir el siguiente paso

**Opción A — Revertir todo el Bloque 5.6 prompts y dejar solo infraestructura.**
- Mantener: PASO 0 (runner multi-run, focused sets, deterministic_metrics), tabla `niveles_por_edad`, tool `niveles.py`, campus tool.
- Revertir: regla "Integridad" en rules.md, regla "TONO ÍNTIMO" en descubrimiento.md, regla "Cuando el papá te corrige" en rules.md, todos los nuevos validators (`no_inventa_datos`, `no_bullets_intimo`).
- Resultado esperado: volver a baseline 57.6% con la infraestructura nueva en lugar.

**Opción B — Bloque 5.7 dirigido (DESPUÉS de reunión con Cecilia).**
- Validar la tabla `niveles_por_edad` con datos correctos de Cecilia (marcar `confirmado_por_cliente=TRUE`).
- Capturar capacidades de aula (ratio niños:educadora) que faltan.
- Replantear validators del Bloque 5.6 como `severity=warning` para que registren pero no regeneren.
- Atacar las dos cosas que faltaron:
  - intimacy_detector más sensible (umbral más bajo en descubrimiento)
  - Handler de "respuesta corta afirmativa con pérdida de contexto" (intent nuevo o reglas adicionales)

**Opción C — Aceptar el resultado y consolidar.**
- Push el bloque como está.
- Documentar las regresiones como deuda.
- Pasar a Bloque 6 (otro foco, ej. handoff a Lily, deploy a producción).
- Iterar Sofía 2.0 con datos reales en producción.

**Mi recomendación**: **A** (revertir prompts + mantener infraestructura) + **B condicional** (esperar Cecilia antes de seguir con cambios pedagógicos).

---

## Estado del repo

- Tests: **306/306** pasando ✅
- ruff check + format: limpios ✅
- 6 commits del Bloque 5.6 (PASO 0 → PASO 5.0)
- Branch: `main`, 6 commits ahead de origin
- Working tree clean salvo este reporte y `tests/golden/results/focused-*.json`

**Próximo paso**: esperar tu decisión entre A/B/C antes de hacer push y/o avanzar.
