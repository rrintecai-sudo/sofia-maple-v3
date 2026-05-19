#!/usr/bin/env python
"""Importa el historial de Sofia v1 (n8n) a `sofia_messages_legacy` y genera
`tests/golden/conversations/*.json` para el golden test runner.

Fuente: `sofia-export/maple-real/chat_histories_sofia_FULL.json` del proyecto
hermano (Proyecto N8N Claude).

Schema de cada mensaje en el export:
  {"id": int, "session_id": "...@s.whatsapp.net",
   "message": {"type": "human"|"ai", "content": "..."},
   "conversacion": "ISO timestamp", "status": null}

Genera:
- INSERT batch en sofia_messages_legacy (vía Management API).
- tests/golden/conversations/<session>.json con {session_id, turns: [...]}.

Idempotente: usa `original_id` para detectar duplicados; si ya está importado,
salta esa fila.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from app.config import get_settings

# Path al export del proyecto hermano
DEFAULT_EXPORT = (
    Path(__file__).resolve().parent.parent.parent
    / "Proyecto N8N Claude"
    / "sofia-export"
    / "maple-real"
    / "chat_histories_sofia_FULL.json"
)
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden" / "conversations"


def _supa_headers(settings: Any) -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/json",
    }


async def existing_original_ids(settings: Any) -> set[int]:
    """Devuelve los original_id ya importados (idempotencia)."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{settings.supabase_url}/rest/v1/sofia_messages_legacy",
            headers=_supa_headers(settings),
            params={"select": "original_id", "limit": "10000"},
        )
    resp.raise_for_status()
    return {row["original_id"] for row in resp.json() if row.get("original_id") is not None}


async def insert_batch(rows: list[dict[str, Any]], settings: Any) -> None:
    """Insert masivo a sofia_messages_legacy."""
    if not rows:
        return
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.supabase_url}/rest/v1/sofia_messages_legacy",
            headers={**_supa_headers(settings), "Prefer": "return=minimal"},
            json=rows,
        )
    if resp.status_code >= 400:
        print(f"❌ HTTP {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
        raise SystemExit(1)


def parse_export(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Export inesperado: se esperaba lista en la raíz")
    return raw


def to_legacy_row(item: dict[str, Any]) -> dict[str, Any]:
    msg = item.get("message") or {}
    role = msg.get("type", "human")
    return {
        "original_id": int(item["id"]),
        "session_id": item.get("session_id", "unknown"),
        "role": role,
        "content": msg.get("content", ""),
        "raw_message": msg,
        "conversacion_at": item.get("conversacion"),
    }


def group_by_session(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        sid = it.get("session_id", "unknown")
        groups[sid].append(it)
    return groups


def _strip_n8n_wrapper(content: str) -> str:
    """Los mensajes 'human' en el export traen un wrapper con 'Mensaje del prospecto:',
    'REGLAS:' etc. Para los golden tests queremos sólo el mensaje real del papá.
    """
    if not content:
        return ""
    # Buscar "Mensaje del prospecto:" — el contenido real viene después, entre comillas
    marker = "Mensaje del prospecto:"
    if marker in content:
        rest = content.split(marker, 1)[1].lstrip()
        # Suele venir como ["texto del usuario"] o "texto del usuario"
        # Tomamos hasta el primer salto de doble línea o "telefono_del_cliente"
        end_markers = ["telefono_del_cliente:", "\n\n"]
        for em in end_markers:
            if em in rest:
                rest = rest.split(em, 1)[0]
                break
        rest = rest.strip()
        # Limpiar wrapper [" ... "] tipo array de Evolution
        if rest.startswith('["') and rest.endswith('"]'):
            rest = rest[2:-2]
        elif rest.startswith("[") and rest.endswith("]"):
            try:
                arr = json.loads(rest)
                if isinstance(arr, list) and arr:
                    rest = " ".join(str(x) for x in arr)
            except Exception:
                pass
        return rest.strip()
    return content.strip()


def build_golden(session_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Construye el JSON golden para una sesión.

    turns: lista alternada de {role, content} en orden cronológico.
    """
    # Orden por id ascendente (cronología real)
    items_sorted = sorted(items, key=lambda x: int(x.get("id", 0)))
    turns: list[dict[str, str]] = []
    for it in items_sorted:
        msg = it.get("message") or {}
        role = msg.get("type", "human")
        # Normalizar: human → user, ai → assistant_original
        role_norm = "user" if role == "human" else "assistant_original"
        content = msg.get("content", "")
        if role_norm == "user":
            content = _strip_n8n_wrapper(content)
        if not content:
            continue
        turns.append({"role": role_norm, "content": content})
    return {
        "session_id": session_id,
        "source": "sofia_v1_n8n_chat_histories",
        "turns": turns,
    }


async def main(
    export_path: Path,
    dry_run: bool,
    skip_db_insert: bool = False,
) -> int:
    settings = get_settings()
    if not skip_db_insert and not settings.supabase_url:
        print("❌ SUPABASE_URL no configurado", file=sys.stderr)
        return 2

    if not export_path.exists():
        print(f"❌ Export no encontrado: {export_path}", file=sys.stderr)
        return 2

    print(f"→ leyendo {export_path.name}")
    items = parse_export(export_path)
    print(f"  {len(items)} mensajes en el export")

    # Insert a Supabase
    if not skip_db_insert:
        existing = await existing_original_ids(settings)
        nuevos = [to_legacy_row(it) for it in items if int(it["id"]) not in existing]
        print(f"→ {len(nuevos)} nuevos (ya en DB: {len(existing)})")
        if dry_run:
            if nuevos:
                print(f"  [DRY] primer row: {nuevos[0]}")
        else:
            # Insert en batches de 100
            for i in range(0, len(nuevos), 100):
                batch = nuevos[i : i + 100]
                print(
                    f"  → insertando batch {i + 1}-{i + len(batch)}/{len(nuevos)}",
                    end="",
                    flush=True,
                )
                await insert_batch(batch, settings)
                print(" ok")
    else:
        print("→ skip DB insert (--skip-db)")

    # Generar golden JSONs
    print(f"→ generando golden JSONs en {GOLDEN_DIR}")
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    groups = group_by_session(items)
    written = 0
    for sid, group in groups.items():
        # Nombre de archivo: prefijar canal whatsapp y limpiar caracteres
        safe = sid.replace("@", "_at_").replace(":", "_")
        out_path = GOLDEN_DIR / f"whatsapp_{safe}.json"
        golden = build_golden(sid, group)
        if dry_run:
            print(f"  [DRY] {out_path.name} ({len(golden['turns'])} turns)")
        else:
            out_path.write_text(json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ✓ {out_path.name} ({len(golden['turns'])} turns)")
        written += 1

    print(f"\n✅ Procesadas {len(items)} mensajes en {written} conversaciones.")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=str(DEFAULT_EXPORT), help="Path al export JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-db", action="store_true", help="Solo generar golden, sin insertar")
    args = parser.parse_args()
    return asyncio.run(main(Path(args.file), dry_run=args.dry_run, skip_db_insert=args.skip_db))


if __name__ == "__main__":
    sys.exit(cli())
