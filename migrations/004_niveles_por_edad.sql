-- ============================================================
-- Migración 004 — Niveles educativos por edad (Bloque 5.6 PASO 2)
--
-- Fuente determinística para que Sofía no invente edades de cada nivel
-- (bug detectado en golden tests: "Infants 3-12 meses" cuando es 18m-2a).
--
-- Los datos seed vienen de docs/AUDIT_FACTUAL_DATA.md, derivados del
-- prompt v2.8. Cecilia debe validar y marcar confirmado_por_cliente=TRUE.
-- Mientras tanto, la tool consulta WHERE vigente = TRUE indistintamente.
-- ============================================================

CREATE TABLE IF NOT EXISTS niveles_por_edad (
    id                       BIGSERIAL PRIMARY KEY,
    nivel                    TEXT NOT NULL UNIQUE,        -- 'cubs_baby', 'baby', 'infants', 'toddlers', 'preschool', 'primaria_baja', etc.
    nombre_display           TEXT NOT NULL,               -- 'Cubs Baby', 'Baby', 'Infants', 'Toddlers', etc.
    categoria                TEXT NOT NULL,               -- 'maternal', 'kinder', 'primaria', 'secundaria'
    edad_min_meses           INT NOT NULL,
    edad_max_meses           INT NOT NULL,
    grados                   TEXT[],                      -- ej. ['1°','2°','3°'] para primaria_baja
    descripcion              TEXT,                        -- foco pedagógico de la etapa (vínculo, exploración, etc.)
    campus                   TEXT,                        -- 'Campus 1' o 'Campus 2'
    vigente                  BOOLEAN NOT NULL DEFAULT TRUE,
    confirmado_por_cliente   BOOLEAN NOT NULL DEFAULT FALSE,  -- ¿Cecilia validó este dato?
    fuente                   TEXT,                            -- 'prompt_v2.8' | 'cecilia_2026-05-XX' | etc.
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_niveles_edad_range ON niveles_por_edad(edad_min_meses, edad_max_meses) WHERE vigente = TRUE;
CREATE INDEX IF NOT EXISTS idx_niveles_categoria ON niveles_por_edad(categoria) WHERE vigente = TRUE;

COMMENT ON TABLE niveles_por_edad IS 'Rangos de edad por nivel educativo. Antes hardcoded en prompt v2.8 (educacion.md). Sofía a veces los re-decía mal — la tool ataca el bug en la fuente.';
COMMENT ON COLUMN niveles_por_edad.confirmado_por_cliente IS 'TRUE = Cecilia (Maple) validó este dato. FALSE = seed inicial desde el prompt actual, pending review.';

-- ============================================================
-- Seed inicial — datos del prompt v2.8
-- Cecilia validará y actualizará confirmado_por_cliente.
-- ============================================================
INSERT INTO niveles_por_edad
    (nivel, nombre_display, categoria, edad_min_meses, edad_max_meses, grados, descripcion, campus, fuente)
VALUES
    ('cubs_baby',     'Cubs Baby',     'maternal',   3,   11,  ARRAY[]::TEXT[], 'Vínculo, seguridad, exploración temprana. Foco: apego.',                                              'Campus 1', 'prompt_v2.8'),
    ('baby',          'Baby',          'maternal',   12,  18,  ARRAY[]::TEXT[], 'Vínculo, motricidad, lenguaje inicial.',                                                              'Campus 1', 'prompt_v2.8'),
    ('infants',       'Infants',       'maternal',   18,  24,  ARRAY[]::TEXT[], 'Exploración, lenguaje, primeros vínculos sociales.',                                                  'Campus 1', 'prompt_v2.8'),
    ('toddlers',      'Toddlers',      'maternal',   24,  36,  ARRAY[]::TEXT[], 'Autonomía emergente, regulación emocional, lenguaje expresivo.',                                      'Campus 1', 'prompt_v2.8'),
    ('preschool',     'Preschool / Kinder', 'kinder', 36, 72,  ARRAY['1°','2°','3°'], 'Aprendizaje activo, juego intencional, amor por aprender.',                                    'Campus 1', 'prompt_v2.8'),
    ('primaria_baja', 'Primaria baja', 'primaria',   72,  108, ARRAY['1°','2°','3°'], 'PBL, Challenge Based Learning, pensamiento crítico básico.',                                   'Campus 1', 'prompt_v2.8'),
    ('primaria_alta', 'Primaria alta', 'primaria',   108, 144, ARRAY['4°','5°','6°'], 'PBL avanzado, argumentación, criterio propio.',                                                'Campus 2', 'prompt_v2.8'),
    ('secundaria',    'Secundaria',    'secundaria', 144, 180, ARRAY['7°','8°','9°'], 'Guía emocional, pensamiento crítico, debate, creatividad. Adolescente con carácter.',           'Campus 2', 'prompt_v2.8')
ON CONFLICT (nivel) DO NOTHING;
