"""Configuración compartida de pytest."""

from __future__ import annotations

import os

import pytest

# Setear ENV=test antes de que se importen settings
os.environ.setdefault("ENV", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")
os.environ.setdefault("SUPABASE_DB_URL", "")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture(autouse=True)
def reset_settings_cache():
    """Limpia el lru_cache de get_settings entre tests para que cada uno
    pueda inyectar su propia config si quiere.
    """
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
