"""Cliente Anthropic con soporte de prompt caching y retry.

Decisión: SDK oficial `anthropic` con cliente async. Caching de bloques de prompt
explícito (cache_control: ephemeral). Ver ARCHITECTURE sección 6.4.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class AnthropicAdapter:
    """Wrapper sobre AsyncAnthropic con configuración inyectada."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: AsyncAnthropic | None = None

    @property
    def client(self) -> AsyncAnthropic:
        """Lazy-init del cliente para no fallar al importar si falta API key."""
        if self._client is None:
            if not self.settings.anthropic_api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY no está configurada. "
                    "Genérala en console.anthropic.com y agrégala al .env"
                )
            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def is_configured(self) -> bool:
        """¿Hay API key configurada? Útil para `/readyz`."""
        return bool(self.settings.anthropic_api_key)

    async def chat(
        self,
        system_blocks: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.5,
    ) -> Message:
        """Llamada principal a Claude con soporte de prompt caching.

        Args:
            system_blocks: lista de bloques de system message. Cada bloque tiene
                la forma `{"type": "text", "text": "...", "cache_control": {...}}`.
                Los bloques estables (identity, rules) van con
                `cache_control={"type": "ephemeral"}` para descuento de 90%.
            messages: historial de mensajes [{role, content}, ...].
            model: override del modelo. Default: settings.anthropic_model_principal.
            max_tokens: límite de tokens de salida.
            temperature: 0.0-1.0.

        Returns:
            anthropic.types.Message con la respuesta completa.
        """
        return await self.client.messages.create(
            model=model or self.settings.anthropic_model_principal,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )

    async def health_check(self) -> dict[str, Any]:
        """Verifica reachability de la API Anthropic.

        Returns:
            dict con `status` ('ok' | 'unauthorized' | 'unreachable' | 'skip')
            y `detail` opcional. No lanza excepciones.
        """
        if not self.is_configured():
            return {"status": "skip", "detail": "no api key configured"}

        try:
            # `messages.create` con max_tokens=1 es la llamada más barata
            # para validar la API key. Pero también podemos sólo hacer un
            # GET a /v1/models que cuesta cero tokens.
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": self.settings.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            if resp.status_code == 200:
                return {"status": "ok"}
            if resp.status_code in (401, 403):
                return {"status": "unauthorized", "detail": f"HTTP {resp.status_code}"}
            return {"status": "unreachable", "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: AnthropicAdapter | None = None


def get_anthropic() -> AnthropicAdapter:
    """Singleton del adapter."""
    global _singleton
    if _singleton is None:
        _singleton = AnthropicAdapter()
    return _singleton
