"""Tests del cálculo de costos."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.observability.costs import (
    PRICING,
    calculate_cost,
    calculate_whisper_cost,
)


def test_calculate_cost_haiku_basic() -> None:
    """Haiku 4.5: 1M input + 1M output = $1.00 + $5.00 = $6.00."""
    cost = calculate_cost(
        model="claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("6.000000")


def test_calculate_cost_haiku_with_cache_read() -> None:
    """Caching: 1M de cache_read es 10% del input = $0.10."""
    cost = calculate_cost(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == Decimal("0.100000")


def test_calculate_cost_gpt4o_mini() -> None:
    """gpt-4o-mini: 1M input = $0.15."""
    cost = calculate_cost(
        model="gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == Decimal("0.150000")


def test_calculate_cost_embeddings() -> None:
    """text-embedding-3-small: 1M tokens = $0.02."""
    cost = calculate_cost(
        model="text-embedding-3-small",
        input_tokens=1_000_000,
    )
    assert cost == Decimal("0.020000")


def test_calculate_cost_unknown_model_raises() -> None:
    with pytest.raises(KeyError, match="Modelo desconocido"):
        calculate_cost(model="modelo-inventado-xyz", input_tokens=100)


def test_whisper_cost() -> None:
    """Whisper: $0.006/min. 60s = $0.006."""
    cost = calculate_whisper_cost(audio_seconds=60.0)
    assert cost == Decimal("0.006000")

    cost = calculate_whisper_cost(audio_seconds=30.0)
    assert cost == Decimal("0.003000")


def test_pricing_has_all_required_models() -> None:
    """Sanity check: los modelos del stack deben estar en PRICING."""
    required = [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "gpt-4o-mini",
        "text-embedding-3-small",
    ]
    for m in required:
        assert m in PRICING, f"Modelo {m} falta en PRICING"


def test_calculate_cost_realistic_turn() -> None:
    """Un turno típico de Sofía: ~5500 tokens input cacheados + 200 output."""
    cost = calculate_cost(
        model="claude-haiku-4-5",
        input_tokens=500,  # parte dinámica (estado, datos volátiles)
        output_tokens=200,
        cache_read_tokens=5000,  # identity+rules+vocabulario+journey cacheado
    )
    # 500/1M * $1 + 200/1M * $5 + 5000/1M * $0.10
    # = $0.0005 + $0.001 + $0.0005 = $0.002
    assert cost == Decimal("0.002000")
