"""Cálculo de costos por turno.

Decisión: tarifas hardcoded en código, versionadas en Git. Ver `docs/DECISIONS.md` ADR-005.
Cuando un proveedor cambia precio, se hace PR a este archivo.

Las tarifas son por **millón de tokens** (estándar de la industria).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPricing:
    """Tarifas por millón de tokens. Vigencia: mayo 2026."""

    input_per_million: Decimal
    output_per_million: Decimal
    cache_write_per_million: Decimal | None = None  # para Anthropic (5m TTL)
    cache_read_per_million: Decimal | None = None  # para Anthropic (90% off)


# ============================================================
# Tarifas — mayo 2026
# Anthropic: https://www.anthropic.com/pricing
# OpenAI:    https://openai.com/api/pricing/
# ============================================================
PRICING: dict[str, ModelPricing] = {
    # Claude (Anthropic)
    "claude-haiku-4-5": ModelPricing(
        input_per_million=Decimal("1.00"),
        output_per_million=Decimal("5.00"),
        cache_write_per_million=Decimal("1.25"),  # +25% sobre input
        cache_read_per_million=Decimal("0.10"),  # 10% del input
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_per_million=Decimal("3.00"),
        output_per_million=Decimal("15.00"),
        cache_write_per_million=Decimal("3.75"),
        cache_read_per_million=Decimal("0.30"),
    ),
    "claude-opus-4-7": ModelPricing(
        input_per_million=Decimal("15.00"),
        output_per_million=Decimal("75.00"),
        cache_write_per_million=Decimal("18.75"),
        cache_read_per_million=Decimal("1.50"),
    ),
    # OpenAI (auxiliares)
    "gpt-4o-mini": ModelPricing(
        input_per_million=Decimal("0.15"),
        output_per_million=Decimal("0.60"),
    ),
    "gpt-4o": ModelPricing(
        input_per_million=Decimal("2.50"),
        output_per_million=Decimal("10.00"),
    ),
    # Embeddings (sólo input, no output)
    "text-embedding-3-small": ModelPricing(
        input_per_million=Decimal("0.02"),
        output_per_million=Decimal("0.00"),
    ),
    "text-embedding-3-large": ModelPricing(
        input_per_million=Decimal("0.13"),
        output_per_million=Decimal("0.00"),
    ),
    # Whisper — se cobra por minuto, no por token. Hardcodeado a $0.006/min.
    "whisper-1": ModelPricing(
        input_per_million=Decimal("0.00"),
        output_per_million=Decimal("0.00"),
    ),
}

WHISPER_USD_PER_MINUTE = Decimal("0.006")


def calculate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Decimal:
    """Calcula el costo de una llamada al modelo en USD.

    Args:
        model: clave de PRICING (ej. 'claude-haiku-4-5').
        input_tokens: tokens de entrada (sin contar los cacheados).
        output_tokens: tokens de salida generados.
        cache_write_tokens: tokens escritos al cache (primera vez).
        cache_read_tokens: tokens leídos del cache (turnos siguientes).

    Returns:
        Costo en USD como Decimal (precisión 6 decimales).

    Raises:
        KeyError: si el modelo no está en PRICING.
    """
    if model not in PRICING:
        raise KeyError(f"Modelo desconocido: {model}. Modelos soportados: {sorted(PRICING)}")

    p = PRICING[model]
    million = Decimal(1_000_000)

    total = (Decimal(input_tokens) * p.input_per_million / million) + (
        Decimal(output_tokens) * p.output_per_million / million
    )

    if cache_write_tokens and p.cache_write_per_million is not None:
        total += Decimal(cache_write_tokens) * p.cache_write_per_million / million

    if cache_read_tokens and p.cache_read_per_million is not None:
        total += Decimal(cache_read_tokens) * p.cache_read_per_million / million

    return total.quantize(Decimal("0.000001"))


def calculate_whisper_cost(audio_seconds: float) -> Decimal:
    """Calcula el costo de transcripción con Whisper."""
    minutes = Decimal(str(audio_seconds)) / Decimal("60")
    return (minutes * WHISPER_USD_PER_MINUTE).quantize(Decimal("0.000001"))
