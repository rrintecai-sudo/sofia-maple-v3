# Auditoría factual de datos en prompts y KB

**Fecha:** 2026-05-19
**Contexto:** PASO 2 del Bloque 5.6. Inventario de todos los datos factuales hardcoded en `app/core/prompts/**/*.md` para validar contra el documento fuente de Maple Collège y mover a tabla `niveles_por_edad` cuando aplique.

**Estado:** Datos detectados son los actuales del prompt v2.8. Cecilia (Maple) debe validar antes de marcar `confirmado_por_cliente=TRUE` en la tabla. Mientras tanto la tool consulta `vigente=TRUE` indistintamente del flag.

---

## Niveles educativos y rangos de edad

Fuente: `app/core/prompts/journey/educacion.md` — sección "Maternal".

| Nivel | Edad min | Edad max | Confirmado por Maple |
|---|---|---|---|
| Cubs Baby | 3 meses | 11 meses | ❓ pending Cecilia |
| Baby | 12 meses | 18 meses | ❓ pending |
| Infants | 18 meses | 24 meses (2 años) | ❓ pending |
| Toddlers | 24 meses (2 años) | ~36 meses (3 años, inferido) | ❓ pending |
| Preschool / Kinder | 3 años | 6 años (inferido) | ❓ pending |
| Primaria baja | 6 años | 9 años (1° a 3°) | ❓ pending |
| Primaria alta | 9 años | 12 años (4° a 6°) | ❓ pending |
| Secundaria | 12 años | 15 años (7° a 9°) | ❓ pending |

**Nota crítica:** El golden test detectó que Sofía v2 a veces dice "Infants 3-12 meses" cuando lo correcto según el prompt es **18 meses a 2 años**. El prompt está bien; el modelo se equivoca. La tool determinística `consultar_nivel_por_edad` ataca esto en la fuente.

---

## Direcciones de campus

Fuente: `app/core/prompts/journey/informacion.md` — sección "CAMPUS".

| Campus | Dirección | Niveles que atiende | Confirmado |
|---|---|---|---|
| Campus 1 | José Figueroa Siller 156, Col. Doctores, Saltillo, Coah. | Maternal, Kinder, Primaria 1°-5° | ❓ pending |
| Campus 2 | Blvd. V. Carranza 5064, Col. Doctores, Saltillo, Coah. | Primaria 6°, Secundaria 7°-9° | ❓ pending |

**Bug detectado:** turno t15 del sid 5218441302 — Sofía da "Blvd. V. Carranza 5064" (Campus 2) cuando el papá venía hablando de secundaria pero el flujo no había confirmado el campus aún. La tool `get_campus_para_nivel` (Bloque 5.5 Fix 4) ya cubre esto si el wiring está activo.

---

## Horarios escolares

Fuente: `app/core/prompts/journey/informacion.md`.

| Nivel | Horario |
|---|---|
| Premater | 9:00–13:00 |
| Mater y 1° Kinder | 9:00–13:00 |
| 2° Kinder | 9:00–14:00 |
| 3° Kinder | 8:30–14:00 |
| 1° a 3° Primaria | 8:00–14:30 |
| 4° a 6° Primaria | 7:50–14:45 |
| Secundaria (7°-9°) | 8:00–14:30 |

**Status:** Hardcoded en prompt. No movido a tabla en Bloque 5.6 (deuda para 5.7+). Existe `app/tools/horarios.py` con seed en Supabase desde Bloque 4.

---

## Costos colegiatura (ciclo 2026-2027)

Fuente: `app/core/prompts/journey/informacion.md` — sección "COSTOS COLEGIATURA".

| Nivel | Inscripción | Colegiatura mensual | Total gastos iniciales |
|---|---|---|---|
| Early Years (Maternal) | $5,000 | $4,900 × 11 | $22,805 |
| Preschool (Kinder) | $10,000 | $5,250 × 11 | $30,405 |
| Primaria baja (1°-3°) | $10,900 | $6,100 × 11 | $25,850 |
| Primaria alta (4°-6°) | $11,300 | $6,300 × 11 | $26,550 |
| Secundaria (7°-9°) | $11,900 | $6,750 × 11 | $30,950 |

**Status:** Hardcoded en prompt + tabla `precios_por_nivel` (Bloque 4) con seed. La tool `app/tools/precios.py` ya cubre esto.

---

## Estancias y costos

Fuente: `app/core/prompts/journey/informacion.md` — sección "ESTANCIAS".

| Modalidad | Aplica a | Precio |
|---|---|---|
| Estancia Completa | Maternal | $2,500/mes |
| Estancia de la mañana | Kinder/Primaria/Secundaria | $550/mes |
| Estancia media | Kinder/Primaria/Secundaria | $1,400/mes |
| Estancia after school | Kinder/Primaria/Secundaria | $3,100/mes |
| Estancia academias | Kinder+ | $630/mes |
| Academias | Kinder+ | $1,000/mes + $1,000 inscripción |
| Estancia express | Cualquiera | $210/día |

**Status:** Hardcoded en prompt + tabla `modalidades_estancia` (Bloque 4). Tool existente.

---

## Capacidades de aula (datos cuantitativos críticos)

| Aspecto | Valor en prompt | Confirmado |
|---|---|---|
| Niños por aula (Maternal) | ❌ NO especificado en prompt | — |
| Ratio educadora:niño Maternal | ❌ NO especificado | — |
| Tamaño grupo Kinder/Primaria | ❌ NO especificado | — |

**Hallazgo crítico:** El prompt NO especifica capacidades de aula. Sofía v2 inventaba "máximo 8 niños" y "máximo 20 niños" en runs distintos (turnos 45, 46, 48). La regla "Integridad de información" (ADR-012) ahora prohíbe inventarlos. Para responder correctamente cuando el papá pregunte "¿cuántos niños por aula?", **Cecilia debe proveer los datos** y los agregamos a `niveles_por_edad.ratio_alumno_educadora`, o crear tabla `capacidades_aula` en 5.7.

Mientras tanto, Sofía debe responder *"Déjame confirmar ese dato exacto con el equipo y te respondo a la brevedad"* — la regla del prompt y el validator `no_inventa_datos` ya cubren esto.

---

## Fechas importantes

| Evento | Fecha | Fuente |
|---|---|---|
| Límite gastos iniciales | 15 de julio 2026 | informacion.md |
| Cargo por incumplimiento | 10% por concepto | informacion.md |
| Ciclo escolar | agosto–junio (11 colegiaturas) | informacion.md |
| Cuota graduación | $1,800 (Toddlers, 3° K, 6° P, 9° S) | informacion.md |

**Status:** Hardcoded. No mover hasta validar con Cecilia.

---

## Niveles que NO existen / no se ofrecen

Fuente: `app/core/prompts/journey/descubrimiento.md`.

- **Preparatoria**: NO disponible para nuevos ingresos.

**Bug detectado:** turnos 58 y 67 — Sofía niega la existencia de "3° de Kinder" o "6to de primaria" sin certeza. Esos niveles SÍ existen (3° Kinder está en la tabla de horarios; 6° primaria está en Campus 2). Bug del modelo, no del dato.

---

## Resumen de acciones tomadas en PASO 2

1. **Nueva migration** `migrations/004_niveles_por_edad.sql` — crea tabla con seed basado en este audit. Columna `confirmado_por_cliente` (BOOL DEFAULT FALSE) para que Cecilia valide.
2. **Nueva tool** `app/tools/niveles.py` con:
   - `consultar_nivel_por_edad(edad_meses: int) -> NivelInfo | None`
   - `consultar_edades_de_nivel(nivel: str) -> NivelInfo | None`
   - `listar_niveles_vigentes() -> list[NivelInfo]`
3. **NO se modifican prompts** — los datos del prompt son correctos según el audit. La validez factual se delega a la tool determinística que ataca el bug en la fuente (modelo se equivoca al re-decir).
4. **Wiring en orchestrator**: pre-fetch de `consultar_nivel_por_edad` cuando intent o palabras clave indican pregunta sobre rango de edad.

## Pendientes para Bloque 5.7 (post-validación con Cecilia)

- Validar tabla `niveles_por_edad` con Cecilia → marcar `confirmado_por_cliente=TRUE`
- Capturar capacidades de aula (ratio niños:educadora) con Cecilia
- Considerar mover precios+horarios hardcoded del prompt a tablas existentes y dejar el prompt apuntando a las tools
