"""Métricas agregadas (helper para endpoints /admin/stats).

Por ahora delgado — sólo el modelo de dato. Implementación real en Bloque 5.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class StatsPeriod(BaseModel):
    """Estadísticas agregadas para un período."""

    from_date: datetime
    to_date: datetime
    total_conversations: int = 0
    total_messages: int = 0
    total_agendados: int = 0
    total_cost_usd: Decimal = Decimal("0")
    avg_messages_per_conversation: float = 0.0
    cost_by_model: dict[str, Decimal] = {}
