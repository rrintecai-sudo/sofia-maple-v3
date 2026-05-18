# Guía de Base de Conocimiento — Sofía 2.0

> Esta guía se llena en Bloque 4 cuando se implementa el pipeline de ingesta.

## Tabla

`documents_maple` en Supabase (pgvector). Schema:

```sql
id              BIGSERIAL PRIMARY KEY,
content         TEXT,            -- chunk de texto
metadata        JSONB,           -- {title, section, subsection, file_name, id_file, pages}
embedding       VECTOR(1536)     -- OpenAI text-embedding-3-small
```

## Ingesta de un documento

```bash
uv run python -m app.ingest.pipeline --file ruta/al.pdf
```

Pipeline:

1. Extracción de texto (pypdf / python-docx / markdown).
2. Limpieza (tildes NFD, comillas tipográficas, bullets).
3. Análisis estructural con Claude Haiku → JSON jerárquico (title, sections, subsections).
4. Chunking semántico (≥3 frases o >500 chars).
5. Clasificación de cada chunk a su sección.
6. Embedding con `text-embedding-3-small` (1536 dims).
7. Insert en `documents_maple`.

## Recomendaciones de contenido

Lo que SÍ va a la KB:

- Manual de padres, FAQ ampliada, descripción por nivel, testimonios, calendario, política de uniforme.

Lo que NO va a la KB (va en tablas dedicadas):

- Precios (`precios_por_nivel`), horarios (`horarios_por_nivel`), gastos iniciales, modalidades de estancia, campus.

Lo que NO va en ningún lado (va en prompts):

- Identidad, journey, prohibiciones, vocabulario, tono.
