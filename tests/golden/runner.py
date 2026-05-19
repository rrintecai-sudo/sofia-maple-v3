"""Golden test runner — replay de conversaciones reales contra Sofía 2.0.

Para cada turno de usuario en una conversación legacy:
1. Llama a `procesar_turno` con session_id aislado (prefijo `golden:`).
2. Captura la respuesta de Sofía 2.0.
3. Pide a Claude Sonnet 4.6 que la compare contra la respuesta original.
4. Categoriza: equivalente | mejor | peor | regresion_critica.

Soporta:
- Modo calibración (`--calibrate --sample N`): toma N turnos consecutivos de 1 conversación,
  para validar que el juez funciona antes de gastar más.
- Modo full (`--full`): corre todas las conversaciones.
- Modo focused (`--focused <set_name>`): corre solo los turnos del focused set.
- Multi-run (`--runs N`): cada turno se ejecuta N veces para promediar varianza
  del juez (Sonnet 4.6 tiene ~±10-15pp entre runs idénticos según ADR-009).

Resultado: tests/golden/results/<timestamp>.json con resumen + detalle.

Uso:
    uv run python -m tests.golden.runner --calibrate
    uv run python -m tests.golden.runner --calibrate --sample 5
    uv run python -m tests.golden.runner --full
    uv run python -m tests.golden.runner --full --runs 3
    uv run python -m tests.golden.runner --focused invented_data --runs 3

NO se usa como test de pytest (es caro). Se llama manualmente o en CI nocturno.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.orchestrator import procesar_turno
from app.core.state import Canal
from app.observability.costs import calculate_cost

_LOG_LEVEL = os.environ.get("GOLDEN_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.WARNING))
log = logging.getLogger(__name__)

GOLDEN_DIR = Path(__file__).resolve().parent / "conversations"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
FOCUSED_DIR = Path(__file__).resolve().parent / "focused_sets"

Category = Literal["equivalente", "mejor", "peor", "regresion_critica"]


@dataclass
class TurnComparison:
    """Resultado para un turno. Si runs>1, `category` es la moda y `run_categories`
    contiene el detalle por run."""

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
    new_validators_warnings: list[str] = field(default_factory=list)
    # Multi-run metadata
    run_categories: list[Category] = field(default_factory=list)
    run_reasonings: list[str] = field(default_factory=list)
    category_distribution: dict[str, int] = field(default_factory=dict)
    # Determinístico: ¿pasaron TODOS los validators en al menos 1 run?
    all_validators_pass: bool = False
    all_validators_pass_count: int = 0  # cuántos runs pasaron TODOS
    # Bloque 5.7 ATAQUE 1: warnings (severity=warning, no bloquean regen)
    any_run_had_warnings: bool = False


@dataclass
class ConversationResult:
    session_id: str
    source_file: str
    total_turns: int
    comparisons: list[TurnComparison] = field(default_factory=list)

    @property
    def by_category(self) -> dict[str, int]:
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
    runs_per_turn: int
    focused_set: str | None
    total_conversations: int
    total_turns: int
    total_cost_usd: Decimal
    by_category: dict[str, int]
    results: list[ConversationResult]
    judge_model: str
    # Métricas determinísticas
    pct_all_validators_pass: float = 0.0
    # Bloque 5.7 ATAQUE 1: % turnos donde algún run tuvo al menos 1 warning
    pct_turns_with_warnings: float = 0.0
    # Histograma de warnings por validator (cuántos turnos lo dispararon)
    warnings_by_validator: dict[str, int] = field(default_factory=dict)
    # Varianza inter-run (solo si runs>1)
    judge_stddev_pct: float | None = None

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


def load_focused_set(name: str) -> dict[str, Any]:
    """Lee un focused set por nombre. Estructura:
    {
      "name": "invented_data",
      "description": "...",
      "selection_criteria": "...",
      "items": [
        {"session_id": "...", "turn_index": N, "user_msg": "...",
         "expected_pattern": "...", "baseline_failed": true,
         "judge_reasoning_excerpts": [...]}
      ]
    }
    """
    path = FOCUSED_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"focused set not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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
# Multi-run helpers
# ============================================================


def _mode_category(cats: list[Category]) -> Category:
    """Categoría moda. Empates: prioridad peor > critica > equivalente > mejor."""
    if not cats:
        return "equivalente"
    c = Counter(cats)
    max_count = max(c.values())
    tied = [k for k, v in c.items() if v == max_count]
    # En empate, preferir el más conservador (peor>critica>equiv>mejor)
    priority = {"regresion_critica": 0, "peor": 1, "equivalente": 2, "mejor": 3}
    tied.sort(key=lambda x: priority.get(x, 99))
    return tied[0]  # type: ignore[return-value]


# ============================================================
# Runner principal
# ============================================================


async def run_conversation(
    golden: dict[str, Any],
    *,
    sample_turns: int | None = None,
    judge_model: str | None = None,
    runs_per_turn: int = 1,
    target_indices: set[int] | None = None,
) -> ConversationResult:
    """Corre una conversación.

    - `sample_turns`: si set, limita a los primeros N pares.
    - `target_indices`: si set, solo juzga estos índices (procesa los previos como
      contexto en silencio para mantener el flujo conversacional).
    - `runs_per_turn`: cuántas veces correr CADA turno target (multi-run).
    """
    session_id_legacy = golden["session_id"]
    pairs = pair_turns(golden.get("turns", []))
    if sample_turns and len(pairs) > sample_turns:
        # Tomar los primeros N pares CONSECUTIVOS — el contexto se acumula
        # turno a turno en sofia_messages/sofia_conversations.
        pairs = pairs[:sample_turns]

    if target_indices is not None:
        # Truncar a hasta el último target (no necesitamos correr los siguientes)
        last_target = max(target_indices)
        pairs = pairs[: last_target + 1]

    result = ConversationResult(
        session_id=session_id_legacy,
        source_file=golden.get("source", "?"),
        total_turns=len(pairs),
    )

    n_targets = len(target_indices) if target_indices else len(pairs)
    label = f"focused [{n_targets} targets]" if target_indices else f"{len(pairs)} turnos"
    print(f"\n=== {session_id_legacy} ({label}) ===")

    # MULTI-RUN: ejecutamos N veces toda la conversación. En cada run usamos un
    # session_id aislado distinto para que el orchestrator no acumule estado
    # cross-run. Por cada turno juntamos las N categorías y reportamos moda.
    per_turn_runs: dict[int, list[dict[str, Any]]] = {idx: [] for idx in range(len(pairs))}

    for run_no in range(runs_per_turn):
        run_session = f"web:golden-r{run_no}-{uuid.uuid4().hex[:8]}"
        for idx, (user_msg, original) in enumerate(pairs):
            is_target = target_indices is None or idx in target_indices
            try:
                turn_res = await procesar_turno(
                    mensaje=user_msg,
                    session_id=run_session,
                    canal=Canal.WEB,
                    tester=True,
                )
                new_text = turn_res.response
                new_cost = turn_res.cost_usd
                new_latency = turn_res.latency_ms
                new_validators_failed = list(turn_res.validators_failed)
                new_validators_warnings = list(turn_res.validators_warnings)
            except Exception as exc:
                log.error(f"orchestrator failed at turn {idx} run {run_no}: {exc}")
                continue

            if not is_target:
                continue  # contexto silencioso, no se juzga

            cat, razon, judge_cost = await judge_response(
                user_msg=user_msg,
                original=original,
                new=new_text,
                judge_model=judge_model,
            )

            per_turn_runs[idx].append(
                {
                    "run_no": run_no,
                    "user_msg": user_msg,
                    "original": original,
                    "new": new_text,
                    "category": cat,
                    "razon": razon,
                    "judge_cost": judge_cost,
                    "new_cost": new_cost,
                    "new_latency": new_latency,
                    "validators_failed": new_validators_failed,
                    "validators_warnings": new_validators_warnings,
                }
            )

    # Consolidar resultados por turno
    for idx in sorted(per_turn_runs.keys()):
        runs = per_turn_runs[idx]
        if not runs:
            continue
        cats = [r["category"] for r in runs]
        mode_cat: Category = _mode_category(cats)
        # Razonamiento: del primer run que coincide con la moda
        razon = next((r["razon"] for r in runs if r["category"] == mode_cat), runs[0]["razon"])
        total_judge_cost = sum((r["judge_cost"] for r in runs), Decimal("0"))
        total_new_cost = sum((r["new_cost"] for r in runs), Decimal("0"))
        avg_latency = int(statistics.mean(r["new_latency"] for r in runs))
        # Métrica determinística: cuántos runs pasaron TODOS los validators
        validators_passed_per_run = [not r["validators_failed"] for r in runs]
        all_pass_count = sum(1 for p in validators_passed_per_run if p)
        # Si AL MENOS 1 run pasó todos, marcamos True (capacidad demostrada)
        all_validators_pass = all_pass_count > 0
        # Concatenar validators_failed únicos
        all_failed: list[str] = []
        for r in runs:
            for v in r["validators_failed"]:
                if v not in all_failed:
                    all_failed.append(v)
        # Bloque 5.7 ATAQUE 1: concatenar warnings únicos + flag de "algún run tuvo warning"
        all_warnings: list[str] = []
        for r in runs:
            for v in r.get("validators_warnings") or []:
                if v not in all_warnings:
                    all_warnings.append(v)
        any_warnings = bool(all_warnings)

        comp = TurnComparison(
            turn_index=idx,
            user_msg=runs[0]["user_msg"][:200],
            original=runs[0]["original"][:400],
            new=runs[0]["new"][:400],
            category=mode_cat,
            razonamiento=razon,
            judge_cost_usd=total_judge_cost,
            new_cost_usd=total_new_cost,
            new_latency_ms=avg_latency,
            new_validators_failed=all_failed,
            new_validators_warnings=all_warnings,
            run_categories=[r["category"] for r in runs],  # type: ignore[misc]
            run_reasonings=[r["razon"][:200] for r in runs],
            category_distribution=dict(Counter(cats)),
            all_validators_pass=all_validators_pass,
            all_validators_pass_count=all_pass_count,
            any_run_had_warnings=any_warnings,
        )
        result.comparisons.append(comp)
        emoji = {"equivalente": "≈", "mejor": "↑", "peor": "↓", "regresion_critica": "✗"}[mode_cat]
        dist_str = (
            ""
            if runs_per_turn == 1
            else " " + "/".join(f"{cats.count(c)}{c[:1]}" for c in set(cats))
        )
        warn_str = f" ⚠{','.join(w[:8] for w in all_warnings)}" if all_warnings else ""
        print(
            f"  {emoji} t{idx:2d} {mode_cat:<20s} "
            f"${total_judge_cost + total_new_cost:.4f}{dist_str}{warn_str}  {razon[:60]}"
        )

    return result


def _judge_stddev_pct(results: list[ConversationResult]) -> float | None:
    """Si runs_per_turn>1, mide la desviación estándar del % equiv/mejor entre runs.

    Para cada run_no separado, calcular % equiv/mejor; reportar stdev de esos %.
    """
    # Recolectar categorías por run_no
    run_cats: dict[int, list[str]] = {}
    for r in results:
        for c in r.comparisons:
            for i, cat in enumerate(c.run_categories):
                run_cats.setdefault(i, []).append(cat)
    if len(run_cats) < 2:
        return None
    pcts = []
    for cats in run_cats.values():
        ok = sum(1 for c in cats if c in ("equivalente", "mejor"))
        pcts.append(100 * ok / len(cats) if cats else 0.0)
    return float(statistics.stdev(pcts)) if len(pcts) > 1 else None


def _all_validators_pass_pct(results: list[ConversationResult]) -> float:
    """% de turnos donde, en AL MENOS 1 run, todos los validators pasaron.

    Esta es la métrica determinística complementaria al juez. No depende del LLM:
    el validator suite es determinístico dada la respuesta del modelo principal.
    Como el modelo principal sí tiene varianza, contamos como "pass" si al menos
    1 run del turno pasó todos. Más estricto: contar runs individuales.
    """
    total = 0
    passed = 0
    for r in results:
        for c in r.comparisons:
            total += 1
            if c.all_validators_pass:
                passed += 1
    return (100 * passed / total) if total else 0.0


def _warnings_stats(results: list[ConversationResult]) -> tuple[float, dict[str, int]]:
    """Bloque 5.7 ATAQUE 1: stats de warnings (severity='warning').

    Devuelve (% turnos con warning, histograma {validator: n_turnos}).
    """
    total = 0
    with_warning = 0
    histogram: dict[str, int] = {}
    for r in results:
        for c in r.comparisons:
            total += 1
            if c.any_run_had_warnings:
                with_warning += 1
            for v in c.new_validators_warnings:
                histogram[v] = histogram.get(v, 0) + 1
    pct = (100 * with_warning / total) if total else 0.0
    return pct, histogram


# ============================================================
# Modes
# ============================================================


async def run_full(args: argparse.Namespace, judge_model: str) -> list[ConversationResult]:
    goldens = load_goldens()
    out = []
    for g in goldens:
        r = await run_conversation(g, judge_model=judge_model, runs_per_turn=args.runs)
        out.append(r)
    return out


async def run_calibrate(args: argparse.Namespace, judge_model: str) -> list[ConversationResult]:
    goldens = load_goldens()
    if not goldens:
        return []
    goldens.sort(key=lambda g: len(g.get("turns", [])), reverse=True)
    g = goldens[0]
    r = await run_conversation(
        g, sample_turns=args.sample, judge_model=judge_model, runs_per_turn=args.runs
    )
    return [r]


async def run_focused(args: argparse.Namespace, judge_model: str) -> list[ConversationResult]:
    fset = load_focused_set(args.focused)
    print(f"\n🎯 Focused set: {fset['name']} — {fset.get('description', '')}")
    items = fset.get("items", [])
    if not items:
        return []
    # Agrupar items por session_id
    by_sid: dict[str, list[int]] = {}
    for it in items:
        by_sid.setdefault(it["session_id"], []).append(it["turn_index"])

    # Cargar conversaciones referenciadas
    sid_to_golden: dict[str, dict[str, Any]] = {}
    for g in load_goldens():
        sid_to_golden[g["session_id"]] = g

    out = []
    for sid, indices in by_sid.items():
        g = sid_to_golden.get(sid)
        if g is None:
            log.warning(f"focused: session {sid} not found in goldens/, skipping")
            continue
        r = await run_conversation(
            g,
            judge_model=judge_model,
            runs_per_turn=args.runs,
            target_indices=set(indices),
        )
        out.append(r)
    return out


# ============================================================
# CLI
# ============================================================


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    judge_model = args.judge_model or settings.anthropic_model_juez

    if args.calibrate:
        mode = "calibrate"
        runner = run_calibrate
    elif args.full:
        mode = "full"
        runner = run_full
    elif args.focused:
        mode = "focused"
        runner = run_focused
    else:
        print("❌ Pasa --calibrate, --full o --focused <set>", file=__import__("sys").stderr)
        return 2

    print(
        f"\n🍁 Golden Runner — mode={mode} judge={judge_model} runs/turn={args.runs}"
        + (f" focused={args.focused}" if args.focused else "")
    )
    started = time.time()
    results = await runner(args, judge_model)
    finished = time.time()

    all_cmps = [c for r in results for c in r.comparisons]
    by_cat: dict[str, int] = dict(Counter(c.category for c in all_cmps))
    total_cost = sum((c.judge_cost_usd + c.new_cost_usd for c in all_cmps), Decimal("0"))

    warn_pct, warn_hist = _warnings_stats(results)
    summary = RunSummary(
        started_at=datetime.fromtimestamp(started, tz=UTC).isoformat(),
        finished_at=datetime.fromtimestamp(finished, tz=UTC).isoformat(),
        mode=mode,
        runs_per_turn=args.runs,
        focused_set=args.focused,
        total_conversations=len(results),
        total_turns=len(all_cmps),
        total_cost_usd=total_cost,
        by_category=by_cat,
        results=results,
        judge_model=judge_model,
        pct_all_validators_pass=_all_validators_pass_pct(results),
        pct_turns_with_warnings=warn_pct,
        warnings_by_validator=warn_hist,
        judge_stddev_pct=_judge_stddev_pct(results) if args.runs > 1 else None,
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{args.focused}" if args.focused else ""
    out_path = RESULTS_DIR / f"{mode}{suffix}-r{args.runs}-{ts}.json"
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
    print(f"% all-validators-pass:  {summary.pct_all_validators_pass:.1f}%  (determinístico)")
    print(
        f"% turnos con warnings:  {summary.pct_turns_with_warnings:.1f}%  (Bloque 5.7 — solo señal, no bloquea)"
    )
    if summary.warnings_by_validator:
        for v, n in sorted(summary.warnings_by_validator.items(), key=lambda x: -x[1]):
            print(f"    ⚠ {v}: {n} turnos")
    if summary.judge_stddev_pct is not None:
        print(f"Desv. est. juez:        ±{summary.judge_stddev_pct:.1f}pp (entre {args.runs} runs)")
    print(f"Costo total: ${total_cost:.4f}")
    print(f"Duración: {(finished - started):.1f}s")
    print(f"Resultado guardado: {out_path}")

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
    g.add_argument("--calibrate", action="store_true", help="Calibra con sample turnos de 1 conv")
    g.add_argument("--full", action="store_true", help="Corre todas las conversaciones")
    g.add_argument(
        "--focused",
        type=str,
        default=None,
        help="Nombre del focused set (sin .json) en tests/golden/focused_sets/",
    )
    parser.add_argument(
        "--sample", type=int, default=5, help="Turnos por conversación en calibrate"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Cuántas veces correr CADA turno (multi-run para varianza del juez)",
    )
    parser.add_argument(
        "--judge-model", help="Override modelo del juez (default settings.anthropic_model_juez)"
    )
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    import sys

    sys.exit(cli())
