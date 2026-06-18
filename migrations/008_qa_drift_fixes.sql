-- ============================================================
-- Migración 008 — Cierre de deriva manual prod↔migraciones (QA)
-- ------------------------------------------------------------
-- `sofia_turn_logs.metadata` existe en producción pero ninguna migración la
-- crea (se agregó a mano). El orchestrator escribe esta columna en cada turno
-- (repository.insert_turn_log), así que un entorno limpio falla con PGRST204.
-- Idempotente.
-- ============================================================

ALTER TABLE sofia_turn_logs ADD COLUMN IF NOT EXISTS metadata JSONB;

-- Forzar recarga del schema cache de PostgREST tras el cambio de columna.
NOTIFY pgrst, 'reload schema';
