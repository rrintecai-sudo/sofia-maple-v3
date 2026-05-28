-- ============================================================
-- Migración 007 — Columna child_grade en leads (Bloque D.3 — Lily 2026-05-27)
--
-- Lily confirmó (nota de voz + captura, reunión 27-may) que ANTES de agendar
-- una cita, Sofía debe capturar 6 datos del lead:
--   1. Nombre del alumno (hijo)        → leads.child_name ✓ ya existe
--   2. Edad                            → leads.child_age  ✓ ya existe
--   3. Grado escolar                   → leads.child_grade ← AGREGAR
--   4. Nombre del papá/mamá            → leads.parent_name ✓ ya existe
--   5. Correo electrónico              → leads.parent_email ✓ ya existe
--   6. Número celular                  → leads.parent_phone ✓ ya existe
--
-- El grado escolar permite resolver campus con precisión (Primaria 1°-5° →
-- Campus 1, Primaria 6° → Campus 2). La edad queda como respaldo.
-- ============================================================

ALTER TABLE leads ADD COLUMN IF NOT EXISTS child_grade TEXT;

COMMENT ON COLUMN leads.child_grade IS 'Grado escolar del hijo en formato textual ("2° primaria", "1ro kinder", etc.). Capturado por Sofía antes de agendar — fuente de verdad para resolver campus (ver app/core/campus_resolver.py).';
