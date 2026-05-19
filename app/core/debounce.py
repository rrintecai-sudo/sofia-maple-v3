"""Debounce de mensajes en cadena con Redis.

Cuando un usuario envía varios mensajes seguidos en WhatsApp/Telegram, queremos
juntarlos en uno solo antes de llamar al LLM (ahorra costos y mejora coherencia).

Patrón (port del flujo de n8n):
1. Llega mensaje → `push_y_marcar(session_id, mensaje)` lo añade a una lista
   y guarda un timestamp + un sequence ID en Redis.
2. Worker espera `window_seconds` (default 7).
3. Worker llama `intentar_reclamar(session_id, seq_id)`: si nadie más reclamó
   y el último mensaje llegó antes de la ventana, devuelve la lista concatenada
   y borra la cola. Si no, devuelve None.
4. Solo el worker que reclamó procesa el turno; los demás abortan.

Web Chat NO usa debounce — los mensajes llegan uno por uno sincronizados.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass

from app.adapters.redis_client import RedisAdapter, get_redis
from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebouncedMessage:
    """Mensaje individual dentro de la cola de debounce."""

    seq_id: str
    content: str
    timestamp: float


@dataclass(frozen=True)
class DebounceClaim:
    """Resultado de un intento de reclamar la cola."""

    claimed: bool
    messages: list[str]
    total_count: int

    @property
    def joined(self) -> str:
        """Mensajes concatenados con saltos de línea, listo para mandar al LLM."""
        return "\n".join(self.messages)


class Debouncer:
    """Coordina ventanas de debounce por session_id sobre Redis."""

    def __init__(
        self,
        redis: RedisAdapter | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self._redis = redis
        s = get_settings()
        self.window_seconds = (
            window_seconds if window_seconds is not None else s.redis_debounce_window_seconds
        )

    @property
    def redis(self) -> RedisAdapter:
        return self._redis or get_redis()

    @staticmethod
    def _msgs_key(session_id: str) -> str:
        return f"debounce:{session_id}:msgs"

    @staticmethod
    def _last_key(session_id: str) -> str:
        return f"debounce:{session_id}:last_seq"

    async def push_message(self, session_id: str, content: str) -> str:
        """Añade un mensaje a la cola y devuelve el seq_id asignado.

        Cada llamada genera un seq_id único. El último mensaje en llegar gana
        el derecho a procesar la ventana (los anteriores ven que su seq no es
        el último y abortan).
        """
        seq_id = uuid.uuid4().hex
        payload = json.dumps({"seq_id": seq_id, "content": content, "timestamp": time.time()})
        msgs_key = self._msgs_key(session_id)
        last_key = self._last_key(session_id)

        client = self.redis.client
        pipe = client.pipeline()
        pipe.rpush(msgs_key, payload)
        pipe.set(last_key, seq_id)
        pipe.expire(msgs_key, self.window_seconds * 4)  # buffer por si crashea el worker
        pipe.expire(last_key, self.window_seconds * 4)
        await pipe.execute()

        log.info(
            "debounce_push",
            extra={"session_id": session_id, "seq_id": seq_id, "len": len(content)},
        )
        return seq_id

    async def try_claim(self, session_id: str, seq_id: str) -> DebounceClaim:
        """Intenta reclamar la cola para procesar.

        Reclama si y solo si:
        - `seq_id` es el último que entró (nadie más ha llegado después).

        Si reclama: borra la cola y last_seq, retorna los mensajes concatenados.
        Si no reclama: retorna lista vacía (alguien más procesará).
        """
        client = self.redis.client
        msgs_key = self._msgs_key(session_id)
        last_key = self._last_key(session_id)

        # Leer el último seq atómicamente
        last_seq = await client.get(last_key)
        if last_seq != seq_id:
            log.info(
                "debounce_not_claimed",
                extra={"session_id": session_id, "seq_id": seq_id, "last": last_seq},
            )
            return DebounceClaim(claimed=False, messages=[], total_count=0)

        # Soy el último — reclamar la cola
        pipe = client.pipeline()
        pipe.lrange(msgs_key, 0, -1)
        pipe.delete(msgs_key)
        pipe.delete(last_key)
        results = await pipe.execute()
        raw_msgs: list[str] = list(results[0]) if results and results[0] else []

        contents: list[str] = []
        for raw in raw_msgs:
            try:
                obj = json.loads(raw)
                content = obj.get("content")
                if isinstance(content, str) and content.strip():
                    contents.append(content)
            except (json.JSONDecodeError, ValueError):
                continue

        log.info(
            "debounce_claimed",
            extra={"session_id": session_id, "count": len(contents)},
        )
        return DebounceClaim(claimed=True, messages=contents, total_count=len(contents))

    async def peek_size(self, session_id: str) -> int:
        """Cuántos mensajes hay en la cola actualmente (para observabilidad)."""
        return await self.redis.client.llen(self._msgs_key(session_id))

    async def clear(self, session_id: str) -> None:
        """Borra la cola y el último seq. Útil para tests / reset manual."""
        client = self.redis.client
        await client.delete(self._msgs_key(session_id))
        await client.delete(self._last_key(session_id))


_singleton: Debouncer | None = None


def get_debouncer() -> Debouncer:
    global _singleton
    if _singleton is None:
        _singleton = Debouncer()
    return _singleton
