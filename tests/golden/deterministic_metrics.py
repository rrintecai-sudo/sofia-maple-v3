"""Métricas determinísticas sobre resultados de golden tests.

Complemento al juez LLM (Sonnet 4.6) que tiene varianza alta entre runs.
Estas métricas son 100% reproducibles dada la misma respuesta de Sofía:

- `pct_all_validators_pass`: % de turnos donde todos los validators pasaron.
- `pct_regenerations`: % de turnos donde hubo al menos 1 regeneración (señal
  débil de calidad: el modelo fue corregido por validators antes de responder).
- `validator_failure_counts`: cuántas veces falló cada validator individual.

Uso programático:
    from tests.golden.deterministic_metrics import compute_from_result_file
    metrics = compute_from_result_file("tests/golden/results/full-r3-2026...json")

Uso CLI:
    uv run python -m tests.golden.deterministic_metrics tests/golden/results/full-r3-2026...json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeterministicMetrics:
    total_turns: int
    pct_all_validators_pass: float
    pct_regenerations: float
    validator_failure_counts: dict[str, int]

    def report(self) -> str:
        lines = [
            f"Total turnos:                      {self.total_turns}",
            f"% all-validators-pass:             {self.pct_all_validators_pass:.1f}%",
            f"% turnos con regeneración:         {self.pct_regenerations:.1f}%",
            "Fallas por validator:",
        ]
        if not self.validator_failure_counts:
            lines.append("  (ninguna)")
        else:
            for v, n in sorted(self.validator_failure_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {v:<32s} {n}")
        return "\n".join(lines)


def compute_from_results(results: list[dict[str, Any]]) -> DeterministicMetrics:
    """Calcula métricas desde una lista de ConversationResult (ya en dict)."""
    total = 0
    passed = 0
    regens = 0
    failure_counter: Counter[str] = Counter()

    for r in results:
        for c in r.get("comparisons", []):
            total += 1
            failed = c.get("new_validators_failed") or []
            if not failed:
                passed += 1
            for v in failed:
                failure_counter[v] += 1
            # `regenerations` no siempre está en el JSON viejo; usar fallback 0
            if c.get("regenerations", 0) > 0:
                regens += 1

    return DeterministicMetrics(
        total_turns=total,
        pct_all_validators_pass=(100 * passed / total) if total else 0.0,
        pct_regenerations=(100 * regens / total) if total else 0.0,
        validator_failure_counts=dict(failure_counter),
    )


def compute_from_result_file(path: str | Path) -> DeterministicMetrics:
    """Calcula métricas desde un archivo de resultado del runner."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return compute_from_results(data.get("results", []))


def cli() -> int:
    if len(sys.argv) < 2:
        print("Uso: python -m tests.golden.deterministic_metrics <result_file.json>")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"❌ no existe: {path}", file=sys.stderr)
        return 2
    metrics = compute_from_result_file(path)
    print(f"\n📊 Métricas determinísticas de {path.name}\n")
    print(metrics.report())
    return 0


if __name__ == "__main__":
    sys.exit(cli())
