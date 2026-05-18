# Guía de prompts — Sofía 2.0

> Esta guía se llena en Bloque 2 cuando se dividen los prompts modulares.

## Estructura

Los prompts viven en `app/core/prompts/` como archivos Markdown con frontmatter YAML.

```
app/core/prompts/
├── identity.md          # SIEMPRE — identidad, journey, principios
├── rules.md             # SIEMPRE — prohibiciones canónicas (sin duplicar)
├── vocabulario.md       # SIEMPRE — argot Maple
├── modo_aprendizaje.md  # Sólo cuando modo=aprendizaje
└── journey/
    ├── bienvenida.md
    ├── descubrimiento.md
    ├── educacion.md
    ├── informacion.md
    ├── objeciones.md
    ├── agendado.md
    └── post_agendado.md
```

## Frontmatter

Cada archivo empieza con:

```markdown
---
file: nombre.md
version: 1.0
last_updated: YYYY-MM-DD
load_when: always | fase=X | modo=Y
estimated_tokens: NNN
---

# Contenido del prompt...
```

## Cómo se compone en runtime

Ver `app/core/prompt_builder.py`. En cada turno se cargan:

1. Bloques siempre (identity, rules, vocabulario) — cacheables.
2. Bloque del fase actual (journey/X.md) — cacheable.
3. Estado capturado dinámico — NO cacheable.

## Workflow de cambios

1. PR a `app/core/prompts/`.
2. Tests golden se corren en el PR.
3. Si pasan, merge a main.
4. Deploy auto a producción.
5. Modo Aprendizaje del equipo genera feedback que va a `sofia_feedback_pending`; un humano lo revisa y crea PR si aplica.
