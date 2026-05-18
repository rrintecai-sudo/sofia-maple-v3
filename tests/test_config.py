"""Tests del módulo config."""

from __future__ import annotations

from app.config import Settings


def test_settings_defaults() -> None:
    """Settings se construye con defaults razonables sin .env."""
    s = Settings()
    assert s.env in ("development", "production", "test")
    assert s.app_port == 8000
    assert s.anthropic_model_principal == "claude-haiku-4-5"
    assert s.openai_model_embeddings == "text-embedding-3-small"
    assert s.openai_embedding_dim == 1536
    assert s.redis_debounce_window_seconds == 7
    assert s.max_regenerations_per_turn == 2


def test_settings_is_production_flag() -> None:
    s = Settings(env="production")
    assert s.is_production is True
    assert s.is_test is False

    s = Settings(env="test")
    assert s.is_test is True
    assert s.is_production is False


def test_max_regenerations_bounded() -> None:
    """max_regenerations debe estar entre 0 y 5."""
    import pytest

    with pytest.raises(ValueError):
        Settings(max_regenerations_per_turn=99)
