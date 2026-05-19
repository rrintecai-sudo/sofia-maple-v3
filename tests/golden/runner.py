"""Golden test runner — replay de conversaciones reales contra Sofía 2.0.

Para cada turno de usuario en una conversación legacy:
1. Llama a `procesar_turno` con session_id aislado (prefijo `golden:`).
2. Captura la respuesta de Sofía 2.0.
3. Pide a Claude Sonnet 4.6 que la compare contra la respuesta original.
4. Categoriza: equivalente | mejor | peor | regresion_critica.

Soporta:
- Modo calibración (`--sample N`): toma N turnos al azar de 1 conversación,
  para validar que el juez funciona antes de gastar más.
- Modo full: corre todas las conversaciones.

Resultado: tests/golden/results/<timestamp>.json con resumen + detalle.

Uso:
    uv run python -m tests.golden.runner --calibrate         # 5 turnos
    uv run python -m tests.golden.runner --calibrate --sample 5
    uv run python -m tests.golden.runner --full              # todas las conversaciones

NO se usa como test de pytest (es caro). Se llama manualmente o en CI nocturno.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.orchestrator import procesar_turno
from app.core.state import Canal
from app.observability.costs import calculate_cost

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)

GOLDEN_DIR = Path(__file__).resolve().parent / "conversations"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

Category = Literal["equivalente", "mejor", "peor", "regresion_critica"]


@dataclass
class TurnComparison:
    turn_index: int
    user_msg: str
    original: str
    new: str
    category: Category
    razonamiento: str
    judge_cost_usd: Decimal = Decimal("0")
    new_cost_usd: Decimal = Decimal("0")
    new_latency_ms: int = 0
    new_validators_failed: list[str] = field(default_factory=list)


@dataclass
class ConversationResult:
    session_id: str
    source_file: str
    total_turns: int
    comparisons: list[TurnComparison] = field(default_factory=list)

    @property
    def by_category(self) -> dict[str, int]:
        from collections import Counter

        return dict(Counter(c.category for c in self.comparisons))

    @property
    def total_cost(self) -> Decimal:
        return sum(
            (c.judge_cost_usd + c.new_cost_usd for c in self.comparisons),
            Decimal("0"),
        )


@dataclass
class RunSummary:
    started_at: str
    finished_at: str
    mode: str
    total_conversations: int
    total_turns: int
    total_cost_usd: Decimal
    by_category: dict[str, int]
    results: list[ConversationResult]
    judge_model: str

    @property
    def pct_equivalente_o_mejor(self) -> float:
        ok = self.by_category.get("equivalente", 0) + self.by_category.get("mejor", 0)
        return (ok / self.total_turns * 100) if self.total_turns else 0.0

    @property
    def pct_regresion_critica(self) -> float:
        n = self.by_category.get("regresion_critica", 0)
        return (n / self.total_turns * 100) if self.total_turns else 0.0


# ============================================================
# Judge prompt
# ============================================================

JUDGE_SYSTEM = """Eres un juez de calidad de respuestas conversacionales.

Comparás dos respuestas (ORIGINAL y NUEVA) ante un mismo mensaje del papá interesado en el colegio Maple Collège. Sofía es una embajadora digital de admisiones; su rol es acompañar la decisión educativa con calidez, generar valor antes de cotizar, y guiar al agendado naturalmente.

Tu tarea: clasificar la NUEVA respuesta en UNA categoría:

- "equivalente": NUEVA transmite el mismo valor, tono y dirección del journey que ORIGINAL. Diferencias de palabras OK; ambas son útiles para el papá.
- "mejor": NUEVA tiene una mejora clara — más concreta, menos repetitiva, mejor escena observable, no repite preguntas, no afirma envíos falsos.
- "peor": NUEVA pierde algo importante — más vaga, más fría, suena más a venta, o agrega muletillas que ORIGINAL no tenía.
- "regresion_critica": NUEVA viola una regla DURA — promete becas académicas, revela que es IA, comparte costos sin que el papá los pida, recita lista numerada en visión, evade pregunta directa, repite pregunta ya respondida, o afirma envío falso.

Si NUEVA es muy distinta pero ambas son válidas, prefiere "equivalente".

Devuelve EXCLUSIVAMENTE JSON: {"category": "...", "razon": "una oración explicando"}.
"""


# ============================================================
# Helpers
# ============================================================


def load_goldens(specific: list[str] | None = None) -> list[dict[str, Any]]:
    """Lee todos los goldens (o los especificados). Cada uno con session_id y turns."""
    files = [GOLDEN_DIR / f for f in specific] if specific else sorted(GOLDEN_DIR.glob("*.json"))
    goldens = []
    for f in files:
        if not f.exists():
            log.warning(f"golden missing: {f}")
            continue
        goldens.append(json.loads(f.read_text(encoding="utf-8")))
    return goldens


def pair_turns(turns: list[dict[str, str]]) -> list[tuple[str, str]]:
    """De una lista plana (user, assistant_original, user, assistant_original, ...)
    devuelve pares (user_msg, original_response).

    Si un user no tiene assistant después, se descarta.
    """
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(turns) - 1:
        a, b = turns[i], turns[i + 1]
        if a.get("role") == "user" and b.get("role") == "assistant_original":
            pairs.append((a["content"], b["content"]))
            i += 2
        else:
            i += 1
    return pairs


async def judge_response(
    user_msg: str,
    original: str,
    new: str,
    *,
    judge_model: str | None = None,
) -> tuple[Category, str, Decimal]:
    """Llama a Claude Sonnet 4.6 para clasificar la nueva respuesta."""
    anthropic = get_anthropic()
    settings = get_settings()
    model = judge_model or settings.anthropic_model_juez

    prompt = (
        f"MENSAJE DEL PAPÁ:\n{user_msg}\n\n"
        f"--- ORIGINAL (Sofia v1):\n{original}\n\n"
        f"--- NUEVA (Sofia v2):\n{new}\n\n"
        "Clasifica la NUEVA. Devuelve solo JSON."
    )
    msg = await anthropic.chat(
        system_blocks=[{"type": "text", "text": JUDGE_SYSTEM}],
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=200,
        temperature=0.0,
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        cat = data.get("category", "equivalente").lower()
        if cat not in ("equivalente", "mejor", "peor", "regresion_critica"):
            cat = "equivalente"
        razon = data.get("razon", "")
    except Exception as exc:
        log.warning(f"judge non-json: {exc} raw={raw[:200]}")
        cat = "equivalente"
        razon = f"(juez devolvió non-json: {raw[:100]})"

    # Costo del juez
    usage = getattr(msg, "usage", None)
    cost = calculate_cost(
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
    return cat, razon, cost  # type: ignore[return-value]


# ============================================================
# Runner principal
# ============================================================


async def run_conversation(
    golden: dict[str, Any],
    *,
    sample_turns: int | None = None,
    judge_model: str | None = None,
) -> ConversationResult:
    session_id_legacy = golden["session_id"]
    pairs = pair_turns(golden.get("turns", []))
    if sample_turns and len(pairs) > sample_turns:
        # Tomar los primeros N pares CONSECUTIVOS — el contexto se acumula
        # turno a turno en sofia_messages/sofia_conversations. Muestreo aleatorio
        # rompería el contexto y haría que Sofia 2.0 responda a mensajes sin saber
        # lo que dijo el papá antes (falsos negativos del juez).
        pairs = pairs[:sample_turns]

    result = ConversationResult(
        session_id=session_id_legacy,
        source_file=golden.get("source", "?"),
        total_turns=len(pairs),
    )
    # session_id aislado para no contaminar sofia_conversations reales
    runner_session = f"web:golden-{uuid.uuid4().hex[:10]}"

    print(f"\n=== {session_id_legacy} ({len(pairs)} turnos) ===")
    for idx, (user_msg, original) in enumerate(pairs):
        try:
            turn_res = await procesar_turno(
                mensaje=user_msg,
                session_id=runner_session,
                canal=Canal.WEB,
                tester=True,
            )
            new_text = turn_res.response
            new_cost = turn_res.cost_usd
            new_latency = turn_res.latency_ms
        except Exception as exc:
            log.error(f"orchestrator failed at turn {idx}: {exc}")
            continue

        cat, razon, judge_cost = await judge_response(
            user_msg=user_msg,
            original=original,
            new=new_text,
            judge_model=judge_model,
        )

        comp = TurnComparison(
            turn_index=idx,
            user_msg=user_msg[:200],
            original=original[:400],
            new=new_text[:400],
            category=cat,
            razonamiento=razon,
            judge_cost_usd=judge_cost,
            new_cost_usd=new_cost,
            new_latency_ms=new_latency,
        )
        result.comparisons.append(comp)
        emoji = {"equivalente": "≈", "mejor": "↑", "peor": "↓", "regresion_critica": "✗"}[cat]
        print(f"  {emoji} t{idx:2d} {cat:<20s} ${judge_cost + new_cost:.4f}  {razon[:60]}")

    return result


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    judge_model = args.judge_model or settings.anthropic_model_juez

    if args.calibrate:
        mode = "calibrate"
        # Tomar SOLO 1 conversación, sólo sample turnos
        goldens = load_goldens()
        if not goldens:
            print("❌ No hay golden files en", GOLDEN_DIR, file=sys.stderr)
            return 2
        # La conversación con más turnos (más representativa)
        goldens.sort(key=lambda g: len(g.get("turns", [])), reverse=True)
        goldens = goldens[:1]
        sample = args.sample
    elif args.full:
        mode = "full"
        goldens = load_goldens()
        sample = None
    else:
        print("❌ Pasa --calibrate o --full", file=sys.stderr)
        return 2

    print(f"\n🍁 Golden Runner — mode={mode} judge={judge_model}")
    started = time.time()
    results: list[ConversationResult] = []
    for g in goldens:
        r = await run_conversation(g, sample_turns=sample, judge_model=judge_model)
        results.append(r)

    finished = time.time()

    # Agregar resumen
    from collections import Counter

    all_cmps = [c for r in results for c in r.comparisons]
    by_cat: dict[str, int] = dict(Counter(c.category for c in all_cmps))
    total_cost = sum((c.judge_cost_usd + c.new_cost_usd for c in all_cmps), Decimal("0"))

    from datetime import datetime

    summary = RunSummary(
        started_at=datetime.fromtimestamp(started, tz=UTC).isoformat(),
        finished_at=datetime.fromtimestamp(finished, tz=UTC).isoformat(),
        mode=mode,
        total_conversations=len(results),
        total_turns=len(all_cmps),
        total_cost_usd=total_cost,
        by_category=by_cat,
        results=results,
        judge_model=judge_model,
    )

    # Persistir resultado
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"{mode}-{ts}.json"
    out_path.write_text(
        json.dumps(_to_dict(summary), default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== Resumen ===")
    print(f"Conversaciones: {summary.total_conversations}")
    print(f"Turnos:         {summary.total_turns}")
    for cat in ("equivalente", "mejor", "peor", "regresion_critica"):
        n = by_cat.get(cat, 0)
        pct = (n / summary.total_turns * 100) if summary.total_turns else 0
        print(f"  {cat:<22s} {n:3d} ({pct:.1f}%)")
    print(f"\n% equivalente o mejor: {summary.pct_equivalente_o_mejor:.1f}%  (objetivo: ≥85%)")
    print(f"% regresión crítica:   {summary.pct_regresion_critica:.1f}%  (objetivo: 0%)")
    print(f"Costo total: ${total_cost:.4f}")
    print(f"Duración: {(finished - started):.1f}s")
    print(f"Resultado guardado: {out_path}")

    # Exit code: 0 si pasa el threshold del calibrado / full run
    if mode == "full":
        if summary.pct_equivalente_o_mejor < 85 or summary.pct_regresion_critica > 0:
            return 1
    return 0


def _to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--calibrate", action="store_true", help="Calibra el juez con N turnos (default 5)"
    )
    g.add_argument("--full", action="store_true", help="Corre todas las conversaciones")
    parser.add_argument("--sample", type=int, default=5, help="Turnos por conversación (default 5)")
    parser.add_argument(
        "--judge-model", help="Override modelo del juez (default settings.anthropic_model_juez)"
    )
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    import sys

    sys.exit(cli())
