"""AI backend factory. Swap backends by editing ai.backend in config.yaml."""

from __future__ import annotations

import logging

from ..config import AIConfig
from .base import AIBackend, AIError

log = logging.getLogger(__name__)


def build_backend(cfg: AIConfig) -> AIBackend:
    if cfg.backend == "anthropic":
        from .anthropic_backend import AnthropicBackend
        log.info("AI backend: Anthropic (%s)", cfg.anthropic.model)
        return AnthropicBackend(
            api_key=cfg.anthropic.api_key,
            model=cfg.anthropic.model,
            max_tokens=cfg.anthropic.max_tokens,
        )
    if cfg.backend == "ollama":
        from .ollama_backend import OllamaBackend
        log.info("AI backend: Ollama (%s @ %s)", cfg.ollama.model,
                 cfg.ollama.base_url)
        return OllamaBackend(
            base_url=cfg.ollama.base_url,
            model=cfg.ollama.model,
            timeout_seconds=cfg.ollama.timeout_seconds,
        )
    raise AIError(f"Unknown AI backend: {cfg.backend!r}")


__all__ = ["AIBackend", "AIError", "build_backend"]
