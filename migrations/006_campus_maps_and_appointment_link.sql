-- ============================================================
-- Migración 006 — Campus + Maps URL + appointment.campus_id (Bloque C.2)
--
-- Cambios:
-- 1. campus.google_maps_url     — link Google Maps por campus
-- 2. appointments.campus_id     — FK a campus (resuelto por nivel del hijo)
-- 3. UPDATE campus con direcciones canónicas (Gaby/Lily 2026-05-24)
-- 4. UPDATE campus.niveles a granularidad por grado (Lily 2026-05-24:
--    Primaria 1-5 → Campus 1, Primaria 6 → Campus 2)
-- ============================================================


-- 1. google_maps_url en campus
ALTER TABLE campus ADD COLUMN IF NOT EXISTS google_maps_url TEXT;

-- 2. campus_id en appointments
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS campus_id BIGINT REFERENCES campus(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_appointments_campus ON appointments(campus_id);

-- 3+4. UPDATE de las dos filas con direcciones canónicas + niveles granulares
-- Campus 1 — Maternal, Kinder (1°/2°/3°), Primaria 1° a 5°
UPDATE campus SET
    nombre = 'Campus 1',
    direccion = 'José Figueroa Siller 156',
    colonia = 'Doctores',
    ciudad = 'Saltillo',
    estado = 'Coahuila',
    pais = 'México',
    niveles = ARRAY[
        'maternal',
        'kinder_1', 'kinder_2', 'kinder_3',
        'primaria_1', 'primaria_2', 'primaria_3', 'primaria_4', 'primaria_5'
    ],
    notas = 'Maternal, Kinder (1°/2°/3°) y Primaria 1° a 5° grado',
    google_maps_url = 'https://www.google.com/maps/search/?api=1&query=Jos%C3%A9+Figueroa+Siller+156%2C+Col.+Doctores%2C+Saltillo%2C+Coahuila',
    vigente = TRUE
WHERE id = 1;

-- Campus 2 — Primaria 6° y Secundaria (1°/2°/3°)
UPDATE campus SET
    nombre = 'Campus 2',
    direccion = 'Blvd. V. Carranza 5064',
    colonia = 'Doctores',
    ciudad = 'Saltillo',
    estado = 'Coahuila',
    pais = 'México',
    niveles = ARRAY[
        'primaria_6',
        'secundaria_1', 'secundaria_2', 'secundaria_3'
    ],
    notas = '6° de Primaria y Secundaria (1°/2°/3°)',
    google_maps_url = 'https://www.google.com/maps/search/?api=1&query=Blvd.+V.+Carranza+5064%2C+Col.+Doctores%2C+Saltillo%2C+Coahuila',
    vigente = TRUE
WHERE id = 2;

COMMENT ON COLUMN campus.google_maps_url IS 'Link Google Maps al campus. Formato Maps URLs API oficial (search?api=1&query=). Se manda al papá al confirmar/aprobar cita.';
COMMENT ON COLUMN appointments.campus_id IS 'Campus resuelto automáticamente desde el nivel del hijo (NUNCA preguntado al papá). Ver app/core/campus_resolver.py.';
