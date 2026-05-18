# syntax=docker/dockerfile:1.7
# ----------------------------------------------------------------------------
# Sofía 2.0 — Dockerfile multi-stage para producción
# ----------------------------------------------------------------------------

# --- Stage 1: builder -------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv viene de la imagen oficial — más rápido que pip install uv
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

# Instalar deps usando uv (cacheable layer)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copiar el código y construir el package
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# Usuario no-root
RUN groupadd --gid 1000 sofia && \
    useradd --uid 1000 --gid sofia --shell /bin/bash --create-home sofia

WORKDIR /app

# Copiar el venv y el código del builder
COPY --from=builder --chown=sofia:sofia /app/.venv /app/.venv
COPY --from=builder --chown=sofia:sofia /app/app /app/app

# Copiar migraciones y scripts para que estén disponibles en runtime
COPY --chown=sofia:sofia migrations /app/migrations
COPY --chown=sofia:sofia scripts /app/scripts
COPY --chown=sofia:sofia web /app/web

USER sofia

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
