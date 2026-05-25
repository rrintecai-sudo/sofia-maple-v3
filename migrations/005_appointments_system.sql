-- ============================================================
-- Migración 005 — Sistema de agendado de citas (Bloque C.1)
--
-- Las tablas `leads`, `appointments` y `activity_events` ya existen en
-- el proyecto Supabase (las creó Maple Platform, app aparte). Sofía
-- escribe directamente en ellas vía Supabase REST y comparte el dato
-- con la plataforma donde Lily aprueba.
--
-- Lo único que crea esta migración: `lily_availability`, la tabla de
-- horarios configurables por Lily. Los enums existentes (appointment_status,
-- lead_stage, event_type, event_actor) ya cubren el flujo:
--
--   appointment_status: pendiente → confirmada (Lily aprueba)
--                       pendiente → cancelada (Lily rechaza)
--                       confirmada → completada / no_show (post-visita)
--
--   lead_stage: contacto_inicial → filtro_completado → cita_agendada
--                → visita_realizada → papeleria_entregada → proceso_iniciado
--                (o descartado en cualquier punto)
--
--   event_type: sofia_appointment_scheduled, lead_stage_changed,
--               lead_note_added, appointment_created, lead_created, etc.
-- ============================================================


-- ============================================================
-- lily_availability — horarios configurables por Lily
-- ============================================================
-- Lily edita desde Maple Platform (UI a construir aparte). Sofía consulta
-- antes de proponer horas al papá. Seeds son placeholders.

CREATE TABLE IF NOT EXISTS lily_availability (
    id                       BIGSERIAL PRIMARY KEY,
    day_of_week              SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time               TIME NOT NULL,
    end_time                 TIME NOT NULL,
    slot_duration_minutes    SMALLINT NOT NULL DEFAULT 60,
    active                   BOOLEAN NOT NULL DEFAULT TRUE,
    notes                    TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT lily_availability_time_order CHECK (end_time > start_time)
);

CREATE INDEX IF NOT EXISTS idx_lily_availability_day_active
    ON lily_availability(day_of_week, active);

COMMENT ON TABLE lily_availability IS 'Horarios laborales de Lily, configurables por ella desde Maple Platform. day_of_week: 0=domingo, 1=lunes, ..., 6=sábado.';

-- Seeds: lun-vie 09:00-17:00, slots de 60 min. Lily ajusta.
INSERT INTO lily_availability (day_of_week, start_time, end_time, slot_duration_minutes, active, notes)
SELECT * FROM (VALUES
    (1::smallint, '09:00'::time, '17:00'::time, 60::smallint, TRUE, 'Lunes — placeholder, Lily ajusta'),
    (2::smallint, '09:00'::time, '17:00'::time, 60::smallint, TRUE, 'Martes — placeholder'),
    (3::smallint, '09:00'::time, '17:00'::time, 60::smallint, TRUE, 'Miércoles — placeholder'),
    (4::smallint, '09:00'::time, '17:00'::time, 60::smallint, TRUE, 'Jueves — placeholder'),
    (5::smallint, '09:00'::time, '17:00'::time, 60::smallint, TRUE, 'Viernes — placeholder')
) AS v(day_of_week, start_time, end_time, slot_duration_minutes, active, notes)
WHERE NOT EXISTS (SELECT 1 FROM lily_availability);
