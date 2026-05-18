-- ============================================================
-- Migración 003 — Modo Aprendizaje + import legacy de n8n
-- ============================================================

-- ============================================================
-- sofia_feedback_pending: feedback del equipo en Modo Aprendizaje
-- IMPORTANTE: no se aplica automáticamente. Requiere PR humano.
-- ============================================================
CREATE TABLE IF NOT EXISTS sofia_feedback_pending (
    id                BIGSERIAL PRIMARY KEY,
    session_id        TEXT NOT NULL,
    feedback_text     TEXT NOT NULL,                         -- lo que dijo Gaby/Lily
    contexto_anterior TEXT,                                  -- mensaje del prospecto + respuesta de Sofía que motivó el feedback
    propuesta_cambio  TEXT,                                  -- propuesta concreta generada por Sofía-modo-aprendizaje
    categoria         TEXT,                                  -- 'tono' | 'precio' | 'objecion' | 'proceso' | 'informacion' | 'prohibicion' | 'otro'
    estado            TEXT NOT NULL DEFAULT 'pending' CHECK (estado IN ('pending', 'approved', 'rejected', 'merged')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revised_by        TEXT,                                  -- email del revisor
    revised_at        TIMESTAMPTZ,
    pr_url            TEXT,                                  -- link al PR si fue aprobado
    notas_revision    TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_pending ON sofia_feedback_pending(estado, created_at DESC) WHERE estado = 'pending';
CREATE INDEX IF NOT EXISTS idx_feedback_session ON sofia_feedback_pending(session_id);

COMMENT ON TABLE sofia_feedback_pending IS 'Feedback recibido en Modo Aprendizaje. NUNCA se auto-aplica al prompt. Un humano revisa y crea PR.';

-- ============================================================
-- sofia_messages_legacy: import de chat_histories_sofia de n8n
-- Sólo lectura — sirve como golden test set.
-- ============================================================
CREATE TABLE IF NOT EXISTS sofia_messages_legacy (
    id              BIGSERIAL PRIMARY KEY,
    original_id     BIGINT,                                  -- id en la tabla original de n8n
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('human', 'ai', 'system')),
    content         TEXT NOT NULL,
    raw_message     JSONB,                                   -- el JSON completo del mensaje original
    conversacion_at TIMESTAMPTZ,
    imported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_legacy_session ON sofia_messages_legacy(session_id, original_id);

COMMENT ON TABLE sofia_messages_legacy IS 'Import de chat_histories_sofia de n8n. 186 mensajes reales, 2 sesiones. Se usan como golden tests para verificar no-regresión.';
