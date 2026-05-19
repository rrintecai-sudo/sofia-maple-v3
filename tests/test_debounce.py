"""Tests del Debouncer (con fakeredis)."""

from __future__ import annotations

import asyncio

import pytest
from app.adapters.redis_client import RedisAdapter
from app.config import Settings
from app.core.debounce import Debouncer
from fakeredis import aioredis as fakeredis_async


@pytest.fixture
async def debouncer():
    """Debouncer apuntando a fakeredis en memoria."""
    adapter = RedisAdapter(settings=Settings(redis_url="redis://fake/0"))
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    adapter._client = fake  # type: ignore[assignment]
    deb = Debouncer(redis=adapter, window_seconds=1)
    yield deb
    await fake.aclose()


@pytest.mark.asyncio
async def test_un_solo_mensaje_se_reclama(debouncer: Debouncer) -> None:
    sid = "telegram:1"
    seq = await debouncer.push_message(sid, "hola")
    claim = await debouncer.try_claim(sid, seq)
    assert claim.claimed is True
    assert claim.messages == ["hola"]
    assert claim.total_count == 1


@pytest.mark.asyncio
async def test_dos_mensajes_solo_ultimo_reclama(debouncer: Debouncer) -> None:
    sid = "telegram:2"
    seq1 = await debouncer.push_message(sid, "hola")
    seq2 = await debouncer.push_message(sid, "tengo una pregunta")

    # El primer worker (seq1) NO debería reclamar — ya hay otro mensaje
    claim1 = await debouncer.try_claim(sid, seq1)
    assert claim1.claimed is False
    assert claim1.messages == []

    # El segundo (seq2) sí reclama y obtiene AMBOS mensajes
    claim2 = await debouncer.try_claim(sid, seq2)
    assert claim2.claimed is True
    assert claim2.messages == ["hola", "tengo una pregunta"]
    assert claim2.total_count == 2


@pytest.mark.asyncio
async def test_claim_concatena_con_saltos(debouncer: Debouncer) -> None:
    sid = "telegram:3"
    await debouncer.push_message(sid, "una")
    await debouncer.push_message(sid, "dos")
    seq = await debouncer.push_message(sid, "tres")
    claim = await debouncer.try_claim(sid, seq)
    assert claim.joined == "una\ndos\ntres"


@pytest.mark.asyncio
async def test_claim_borra_cola(debouncer: Debouncer) -> None:
    sid = "telegram:4"
    seq = await debouncer.push_message(sid, "test")
    await debouncer.try_claim(sid, seq)
    # Tras reclamar, la cola queda vacía
    assert await debouncer.peek_size(sid) == 0


@pytest.mark.asyncio
async def test_clear_borra_cola(debouncer: Debouncer) -> None:
    sid = "telegram:5"
    await debouncer.push_message(sid, "a")
    await debouncer.push_message(sid, "b")
    assert await debouncer.peek_size(sid) == 2
    await debouncer.clear(sid)
    assert await debouncer.peek_size(sid) == 0


@pytest.mark.asyncio
async def test_sessions_aisladas(debouncer: Debouncer) -> None:
    """Mensajes de session distintas no interfieren."""
    seq_a = await debouncer.push_message("telegram:A", "hola desde A")
    seq_b = await debouncer.push_message("telegram:B", "hola desde B")

    claim_a = await debouncer.try_claim("telegram:A", seq_a)
    claim_b = await debouncer.try_claim("telegram:B", seq_b)
    assert claim_a.claimed and claim_a.messages == ["hola desde A"]
    assert claim_b.claimed and claim_b.messages == ["hola desde B"]


@pytest.mark.asyncio
async def test_payload_invalido_se_descarta(debouncer: Debouncer) -> None:
    """Si Redis tuviera basura, no debe explotar."""
    sid = "telegram:6"
    # Push un payload válido + uno corrupto manualmente
    await debouncer.redis.client.rpush(debouncer._msgs_key(sid), "no-json")
    seq = await debouncer.push_message(sid, "valido")
    claim = await debouncer.try_claim(sid, seq)
    assert claim.claimed is True
    # Solo el válido aparece, el corrupto se descarta
    assert claim.messages == ["valido"]


@pytest.mark.asyncio
async def test_race_condition_simulada(debouncer: Debouncer) -> None:
    """Dos pushes casi simultáneos: solo el último claim devuelve todo."""
    sid = "telegram:race"

    async def push_concurrent(content: str) -> str:
        await asyncio.sleep(0.01)
        return await debouncer.push_message(sid, content)

    seqs = await asyncio.gather(
        push_concurrent("mensaje 1"),
        push_concurrent("mensaje 2"),
        push_concurrent("mensaje 3"),
    )
    # Solo el último (cualquiera que termine al final) reclama
    claims = await asyncio.gather(*(debouncer.try_claim(sid, s) for s in seqs))
    claimed = [c for c in claims if c.claimed]
    not_claimed = [c for c in claims if not c.claimed]
    assert len(claimed) == 1
    assert len(not_claimed) == 2
    assert claimed[0].total_count == 3
