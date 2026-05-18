-- ============================================================
-- Migración 001 — Esquema inicial Sofía 2.0
-- Tablas: sofia_conversations, sofia_messages, sofia_turn_logs
-- ============================================================

-- Extensión pgvector (KB ya la usa para documents_maple)
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- sofia_conversations: una fila por sesión (papá × canal)
-- ============================================================
CREATE TABLE IF NOT EXISTS sofia_conversations (
    session_id        TEXT PRIMARY KEY,                    -- prefijado por canal: 'whatsapp:5218...', 'telegram:123...', 'web:<uuid>'
    canal             TEXT NOT NULL CHECK (canal IN ('whatsapp', 'telegram', 'web')),
    identificador     TEXT NOT NULL,                       -- número WhatsApp, telegram chat_id, web uuid
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    estado_capturado  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- nivel_buscado, nombre_hijo, edad_hijo, escuela_actual, miedos, etc.
    frases_usadas     TEXT[] NOT NULL DEFAULT '{}',        -- para anti-repetición de munición
    fase_journey      TEXT,                                -- bienvenida|descubrimiento|educacion|informacion|objeciones|agendado|post_agendado
    agendado          BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_agendado    TIMESTAMPTZ,
    modo              TEXT NOT NULL DEFAULT 'normal' CHECK (modo IN ('normal', 'aprendizaje')),
    notas_internas    TEXT,
    tester            BOOLEAN NOT NULL DEFAULT FALSE       -- TRUE si es Oscar/Lily/Gaby
);

CREATE INDEX IF NOT EXISTS idx_sofia_conv_updated     ON sofia_conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sofia_conv_canal       ON sofia_conversations(canal);
CREATE INDEX IF NOT EXISTS idx_sofia_conv_fase        ON sofia_conversations(fase_journey);
CREATE INDEX IF NOT EXISTS idx_sofia_conv_agendado    ON sofia_conversations(agendado) WHERE agendado = TRUE;
CREATE INDEX IF NOT EXISTS idx_sofia_conv_modo        ON sofia_conversations(modo) WHERE modo = 'aprendizaje';

COMMENT ON TABLE sofia_conversations IS 'Una fila por sesión de WhatsApp/Telegram/Web. estado_capturado contiene los datos extraídos del usuario para evitar pedirlos dos veces.';

-- ============================================================
-- sofia_messages: una fila por mensaje (user o assistant)
-- ============================================================
CREATE TABLE IF NOT EXISTS sofia_messages (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sofia_conversations(session_id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    tipo            TEXT NOT NULL DEFAULT 'texto' CHECK (tipo IN ('texto', 'audio', 'imagen', 'sticker', 'documento')),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tokens_input    INT,
    tokens_output   INT,
    cost_usd        NUMERIC(10, 6),
    model_used      TEXT,
    cache_hit       BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms      INT
);

CREATE INDEX IF NOT EXISTS idx_sofia_msg_session ON sofia_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sofia_msg_created ON sofia_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sofia_msg_role    ON sofia_messages(session_id, role);

COMMENT ON TABLE sofia_messages IS 'Historial de mensajes. role=user para el papá, role=assistant para Sofía. Costos por mensaje permiten breakdown por sesión.';

-- ============================================================
-- sofia_turn_logs: trazabilidad detallada (debug / observabilidad)
-- ============================================================
CREATE TABLE IF NOT EXISTS sofia_turn_logs (
    id                BIGSERIAL PRIMARY KEY,
    session_id        TEXT NOT NULL,
    turn_number       INT NOT NULL,
    user_message      TEXT,
    intent            TEXT,
    rag_chunks        JSONB,                       -- chunks recuperados (id, score, content_preview)
    tools_used        TEXT[] NOT NULL DEFAULT '{}',
    prompt_compuesto  TEXT,                        -- prompt EXACTO enviado al LLM
    llm_response      TEXT,                        -- respuesta cruda del LLM (antes de validators)
    validators_passed JSONB NOT NULL DEFAULT '{}'::jsonb,
    validators_failed JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_response    TEXT,                        -- respuesta enviada al usuario (post validators)
    regenerations     INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tokens_input      INT,
    tokens_output     INT,
    tokens_cached     INT,
    cost_usd          NUMERIC(10, 6),
    latency_ms        INT,
    model_used        TEXT
);

CREATE INDEX IF NOT EXISTS idx_sofia_log_session ON sofia_turn_logs(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_sofia_log_created ON sofia_turn_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sofia_log_intent  ON sofia_turn_logs(intent);

COMMENT ON TABLE sofia_turn_logs IS 'Una fila por turno completo. prompt_compuesto y llm_response permiten debug exacto de qué pasó. validators_failed sirve para detectar problemas sistémicos.';

-- ============================================================
-- Trigger: actualizar updated_at en sofia_conversations
-- ============================================================
CREATE OR REPLACE FUNCTION update_sofia_conv_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sofia_conv_updated_at ON sofia_conversations;
CREATE TRIGGER trg_sofia_conv_updated_at
BEFORE UPDATE ON sofia_conversations
FOR EACH ROW
EXECUTE FUNCTION update_sofia_conv_updated_at();
