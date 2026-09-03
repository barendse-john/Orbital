"""Anthropic Messages API over plain HTTP (no SDK, keeps the Pi install light)."""

from __future__ import annotations

import logging

import httpx

from .base import AIBackend, AIError

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicBackend(AIBackend):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 600):
        if not api_key:
            raise AIError("Anthropic backend needs an API key")
        self.model = model
        self.default_max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def complete(
        self, system: str, user: str, *, max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            resp = await self._client.post(API_URL, json=payload)
        except httpx.HTTPError as exc:
            raise AIError(f"Anthropic request failed: {exc}") from exc

        if resp.status_code == 401:
            raise AIError("Anthropic rejected the API key (401)")
        if resp.status_code == 429:
            raise AIError("Anthropic rate limit hit (429)")
        if resp.status_code >= 400:
            raise AIError(f"Anthropic error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise AIError("Anthropic returned an empty response")
        return text

    async def close(self) -> None:
        await self._client.aclose()
