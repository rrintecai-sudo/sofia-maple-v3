"""Búsqueda semántica en la base de conocimiento (Supabase pgvector).

Usa `text-embedding-3-small` para embedding la query, luego llama al RPC
`match_documents` (que ya existe en Supabase de los técnicos anteriores) o
hace un similarity search vía PostgREST.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.adapters.openai_client import get_openai
from app.config import Settings, get_settings

log = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
DEFAULT_TABLE = "documents_maple"
DEFAULT_RPC = "match_documents"


@dataclass(frozen=True)
class KbResult:
    """Chunk recuperado del vector store."""

    id: int | None
    content: str
    metadata: dict[str, Any]
    similarity: float | None = None

    @property
    def section(self) -> str | None:
        return self.metadata.get("section")

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")


async def kb_search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    threshold: float | None = None,
    table: str = DEFAULT_TABLE,
    settings: Settings | None = None,
) -> list[KbResult]:
    """Busca los `top_k` chunks más similares a `query` en `documents_maple`.

    Returns:
        Lista de KbResult ordenada por similarity descendente.
        Vacía si no se puede embeber o si Supabase falla — falla suavemente.
    """
    settings = settings or get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        log.warning("kb_search: supabase no configurado")
        return []

    openai = get_openai()
    if not openai.is_configured():
        log.warning("kb_search: openai no configurado")
        return []

    try:
        embeddings = await openai.embed([query], model=settings.openai_model_embeddings)
    except Exception as exc:
        log.warning("kb_search embed failed", extra={"error": str(exc)})
        return []

    if not embeddings or not embeddings[0]:
        return []
    vector = embeddings[0]

    # Intento 1: RPC match_documents (existe en Supabase del técnico anterior)
    rpc_results = await _try_rpc_match(vector, top_k, threshold, settings)
    if rpc_results is not None:
        return rpc_results

    # Intento 2: PostgREST fallback (lee toda la tabla y compara en cliente — solo
    # útil para KBs pequeñas; con 16 chunks va bien). Si la KB crece >100 chunks,
    # toca crear un RPC custom.
    return await _fallback_postgrest(vector, top_k, table, settings)


async def _try_rpc_match(
    vector: list[float],
    top_k: int,
    threshold: float | None,
    settings: Settings,
) -> list[KbResult] | None:
    """Intenta el RPC `match_documents` si existe. Retorna None si el RPC falla."""
    payload: dict[str, Any] = {
        "query_embedding": vector,
        "match_count": top_k,
    }
    if threshold is not None:
        payload["match_threshold"] = threshold

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.supabase_url}/rest/v1/rpc/{DEFAULT_RPC}",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code != 200:
            log.info(
                "kb_search rpc fallback",
                extra={"status": resp.status_code, "detail": resp.text[:200]},
            )
            return None
        rows = resp.json() or []
    except Exception as exc:
        log.warning("kb_search rpc failed", extra={"error": str(exc)})
        return None

    return [
        KbResult(
            id=row.get("id"),
            content=row.get("content", ""),
            metadata=row.get("metadata") or {},
            similarity=row.get("similarity"),
        )
        for row in rows
    ]


async def _fallback_postgrest(
    vector: list[float],
    top_k: int,
    table: str,
    settings: Settings,
) -> list[KbResult]:
    """Fallback: lee la tabla y hace cosine similarity en cliente.

    Solo viable para KBs pequeñas (<1000 chunks). Bloque 4 KB tiene ~16 chunks.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{settings.supabase_url}/rest/v1/{table}",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
                params={"select": "id,content,metadata,embedding", "limit": "500"},
            )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("kb_search fallback postgrest failed", extra={"error": str(exc)})
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        emb_raw = row.get("embedding")
        emb = _parse_embedding(emb_raw)
        if emb is None or len(emb) != len(vector):
            continue
        sim = _cosine_similarity(vector, emb)
        scored.append((sim, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        KbResult(
            id=row.get("id"),
            content=row.get("content", ""),
            metadata=row.get("metadata") or {},
            similarity=sim,
        )
        for sim, row in scored[:top_k]
    ]


def _parse_embedding(raw: Any) -> list[float] | None:
    """Postgres `vector` puede llegar como string '[0.1,0.2,...]' o como lista."""
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        import ast

        try:
            value = ast.literal_eval(raw)
            if isinstance(value, list):
                return [float(x) for x in value]
        except (ValueError, SyntaxError):
            pass
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Similaridad coseno entre dos vectores. Asume mismo largo."""
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
