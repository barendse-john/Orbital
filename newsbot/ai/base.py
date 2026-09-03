"""The interface every AI backend implements.

Backends are deliberately dumb: text in, text out. All prompt logic lives in
newsbot/brain.py, so switching between Anthropic and Ollama changes nothing
about how the bot behaves.
"""

from __future__ import annotations

import abc


class AIError(RuntimeError):
    """The backend could not produce a response."""


class AIBackend(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> str:
        """Return the model's text response. Raises AIError on failure."""

    async def health(self) -> bool:
        """Cheap liveness probe used by /status."""
        try:
            await self.complete("Reply with OK.", "ping", max_tokens=8)
            return True
        except Exception:  # noqa: BLE001 - health checks never raise
            return False

    async def close(self) -> None:
        """Release any network resources."""
