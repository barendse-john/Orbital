"""Anthropic Messages API over plain HTTP (no SDK, keeps the Pi install light)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .base import AIBackend, AIError, describe

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# A digest is built once a day and nobody is watching it happen, so a slow
# night on the Pi's connection should cost a few seconds, not the summaries.
BACKOFF_SECONDS = (1.0, 4.0)
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}


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
        temperature: float = 0.0, attempts: int | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        allowed = len(BACKOFF_SECONDS) + 1 if attempts is None \
            else max(1, attempts)
        resp = None
        for attempt in range(allowed):
            last_try = attempt >= allowed - 1
            wait = None if last_try or attempt >= len(BACKOFF_SECONDS) \
                else BACKOFF_SECONDS[attempt]
            try:
                resp = await self._client.post(API_URL, json=payload)
            except httpx.HTTPError as exc:
                failure = AIError(f"Anthropic request failed: {describe(exc)}")
            else:
                if resp.status_code not in RETRY_STATUSES:
                    break
                failure = AIError(
                    f"Anthropic error {resp.status_code}: "
                    f"{resp.text[:200] or '(no body)'}"
                )
                wait = _retry_after(resp, wait)

            if wait is None:
                raise failure
            log.warning("%s - retrying in %.0fs", failure, wait)
            await asyncio.sleep(wait)

        if resp.status_code == 401:
            raise AIError("Anthropic rejected the API key (401)")
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


def _retry_after(resp, default: float | None) -> float | None:
    """Honour the server's own backoff, within reason."""
    if default is None:
        return None
    try:
        asked = float(resp.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return default
    return max(default, min(asked, 30.0))
