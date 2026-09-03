"""Local Ollama backend - zero cost, no network, slower on a Pi."""

from __future__ import annotations

import logging

import httpx

from .base import AIBackend, AIError

log = logging.getLogger(__name__)


class OllamaBackend(AIBackend):
    name = "ollama"

    def __init__(
        self, base_url: str = "http://localhost:11434",
        model: str = "llama3.2:3b", timeout_seconds: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout_seconds), connect=10.0)
        )

    async def complete(
        self, system: str, user: str, *, max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "system": system,
            "prompt": user,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            resp = await self._client.post(f"{self.base_url}/api/generate",
                                           json=payload)
        except httpx.HTTPError as exc:
            raise AIError(
                f"Ollama unreachable at {self.base_url} ({exc}). Is it running?"
            ) from exc

        if resp.status_code == 404:
            raise AIError(
                f"Ollama has no model named {self.model!r}. "
                f"Run: ollama pull {self.model}"
            )
        if resp.status_code >= 400:
            raise AIError(f"Ollama error {resp.status_code}: {resp.text[:300]}")

        text = (resp.json().get("response") or "").strip()
        if not text:
            raise AIError("Ollama returned an empty response")
        return text

    async def close(self) -> None:
        await self._client.aclose()
