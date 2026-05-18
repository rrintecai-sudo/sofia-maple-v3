"""Cliente OpenAI para modelos auxiliares (clasificación, extractor, embeddings, vision)."""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


class OpenAIAdapter:
    """Wrapper sobre AsyncOpenAI."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self.settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY no está configurada. Agrégala al .env")
            self._client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        return self._client

    def is_configured(self) -> bool:
        return bool(self.settings.openai_api_key)

    async def classify(
        self,
        text: str,
        instructions: str,
        model: str | None = None,
    ) -> str:
        """Wrapper genérico para clasificación con structured output.

        Retorna el contenido de la respuesta (string). El caller parsea JSON si aplica.
        """
        completion = await self.client.chat.completions.create(
            model=model or self.settings.openai_model_auxiliar,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        return completion.choices[0].message.content or ""

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Genera embeddings. Devuelve uno por cada texto en el mismo orden."""
        response = await self.client.embeddings.create(
            model=model or self.settings.openai_model_embeddings,
            input=texts,
        )
        return [item.embedding for item in response.data]

    async def health_check(self) -> dict[str, Any]:
        """Verifica reachability de OpenAI."""
        if not self.is_configured():
            return {"status": "skip", "detail": "no api key configured"}

        try:
            # GET /v1/models es barato y valida la key
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                )
            if resp.status_code == 200:
                return {"status": "ok"}
            if resp.status_code in (401, 403):
                return {"status": "unauthorized", "detail": f"HTTP {resp.status_code}"}
            return {"status": "unreachable", "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"status": "unreachable", "detail": str(exc)}


_singleton: OpenAIAdapter | None = None


def get_openai() -> OpenAIAdapter:
    global _singleton
    if _singleton is None:
        _singleton = OpenAIAdapter()
    return _singleton
