-- ============================================================
-- Migración 002 — Conocimiento volátil
-- Lo que antes estaba hardcoded en el prompt: precios, horarios,
-- gastos iniciales, campus, modalidades de estancia, becas.
-- ============================================================

-- ============================================================
-- precios_por_nivel: colegiatura, inscripción, etc. por ciclo
-- ============================================================
CREATE TABLE IF NOT EXISTS precios_por_nivel (
    id                   BIGSERIAL PRIMARY KEY,
    ciclo_escolar        TEXT NOT NULL,                     -- '2026-2027'
    nivel                TEXT NOT NULL,                     -- 'maternal' | 'kinder' | 'primaria_baja' | 'primaria_alta' | 'secundaria'
    sub_nivel            TEXT,                              -- 'early_years' | 'preschool' | '1-3' | '4-6' | '7-9'
    inscripcion          NUMERIC(10, 2),
    colegiatura_mensual  NUMERIC(10, 2),
    seguro_escolar       NUMERIC(10, 2),
    seguro_orfandad      NUMERIC(10, 2),
    recursos_educativos  NUMERIC(10, 2),
    gastos_escolares     NUMERIC(10, 2),
    desayunos_snacks     NUMERIC(10, 2),
    talleres             NUMERIC(10, 2),
    cuota_graduacion     NUMERIC(10, 2),
    total_gastos_iniciales NUMERIC(10, 2),
    num_colegiaturas     INT NOT NULL DEFAULT 11,
    fecha_limite_pago    DATE,
    vigente              BOOLEAN NOT NULL DEFAULT TRUE,
    notas                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ciclo_escolar, nivel, sub_nivel)
);

CREATE INDEX IF NOT EXISTS idx_precios_vigentes ON precios_por_nivel(ciclo_escolar, nivel) WHERE vigente = TRUE;

COMMENT ON TABLE precios_por_nivel IS 'Costos por ciclo y nivel. Antes hardcoded en system prompt. Cambia cada ciclo escolar — actualización vía SQL update.';

-- ============================================================
-- horarios_por_nivel: horario escolar regular y modalidades especiales
-- ============================================================
CREATE TABLE IF NOT EXISTS horarios_por_nivel (
    id           BIGSERIAL PRIMARY KEY,
    nivel        TEXT NOT NULL,                              -- 'premater' | 'maternal' | 'kinder_1' | 'kinder_2' | 'kinder_3' | 'primaria_baja' | 'primaria_alta' | 'secundaria'
    modalidad    TEXT NOT NULL DEFAULT 'regular',            -- 'regular' | 'extendido' | 'estancia'
    hora_inicio  TIME NOT NULL,
    hora_fin     TIME NOT NULL,
    dias         TEXT NOT NULL DEFAULT 'L-V',
    notas        TEXT,
    vigente      BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_horarios_vigentes ON horarios_por_nivel(nivel, modalidad) WHERE vigente = TRUE;

-- ============================================================
-- modalidades_estancia: horario extendido (ciclo 2026-2027)
-- ============================================================
CREATE TABLE IF NOT EXISTS modalidades_estancia (
    id                  BIGSERIAL PRIMARY KEY,
    ciclo_escolar       TEXT NOT NULL,
    nombre              TEXT NOT NULL,                       -- 'completa', 'manana', 'media', 'after_school', 'academias', 'express'
    aplica_para         TEXT[] NOT NULL,                     -- ['maternal'] o ['kinder', 'primaria', 'secundaria']
    hora_inicio         TIME,
    hora_fin            TIME,
    incluye_comida      BOOLEAN NOT NULL DEFAULT FALSE,
    incluye_snack       BOOLEAN NOT NULL DEFAULT FALSE,
    incluye_academia    BOOLEAN NOT NULL DEFAULT FALSE,
    costo_mensual       NUMERIC(10, 2),
    costo_por_dia       NUMERIC(10, 2),
    inscripcion_extra   NUMERIC(10, 2),
    notas               TEXT,
    vigente             BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (ciclo_escolar, nombre)
);

-- ============================================================
-- campus: direcciones e info por nivel
-- ============================================================
CREATE TABLE IF NOT EXISTS campus (
    id           BIGSERIAL PRIMARY KEY,
    nombre       TEXT NOT NULL,                              -- 'Campus 1' | 'Campus 2'
    direccion    TEXT NOT NULL,
    colonia      TEXT,
    ciudad       TEXT NOT NULL DEFAULT 'Saltillo',
    estado       TEXT NOT NULL DEFAULT 'Coahuila',
    pais         TEXT NOT NULL DEFAULT 'México',
    niveles      TEXT[] NOT NULL,                            -- niveles que atiende
    notas        TEXT,
    vigente      BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============================================================
-- becas: tipos de beca y porcentajes
-- ============================================================
CREATE TABLE IF NOT EXISTS becas (
    id           BIGSERIAL PRIMARY KEY,
    tipo         TEXT NOT NULL UNIQUE,                       -- 'hermanos_2do', 'hermanos_3ro', 'socioeconomica'
    porcentaje   NUMERIC(5, 2),                              -- 10.00, 15.00 o NULL para socioeconómica
    descripcion  TEXT NOT NULL,
    condiciones  TEXT,
    vigente      BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============================================================
-- Trigger comun para updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION update_volatile_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_precios_updated ON precios_por_nivel;
CREATE TRIGGER trg_precios_updated BEFORE UPDATE ON precios_por_nivel
    FOR EACH ROW EXECUTE FUNCTION update_volatile_updated_at();

DROP TRIGGER IF EXISTS trg_horarios_updated ON horarios_por_nivel;
CREATE TRIGGER trg_horarios_updated BEFORE UPDATE ON horarios_por_nivel
    FOR EACH ROW EXECUTE FUNCTION update_volatile_updated_at();
