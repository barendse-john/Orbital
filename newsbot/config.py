"""Configuration: config.yaml for behaviour, .env for secrets.

Any ${VAR} in the YAML is replaced with the environment variable of that name
(loaded from .env if present), so no key ever has to live in the config file.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised when the config file is missing or unusable."""


def _expand(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with environment values."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        out = _ENV_RE.sub(sub, value)
        return out.strip()
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class TelegramConfig:
    token: str = ""
    whitelist: list[int] = field(default_factory=list)
    admins: list[int] = field(default_factory=list)


@dataclass
class AnthropicConfig:
    api_key: str = ""
    model: str = "claude-haiku-4-5"
    max_tokens: int = 600


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.2:3b"
    timeout_seconds: int = 120


@dataclass
class AIConfig:
    backend: str = "anthropic"
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)


@dataclass
class GNewsConfig:
    api_key: str = ""
    daily_quota: int = 100


@dataclass
class NewsConfig:
    language: str = "en"
    country: str = "us"
    lookback_hours: int = 24
    max_articles_per_topic: int = 5
    # Hard ceiling across every topic, so one digest stays one message.
    max_articles_total: int = 6
    gnews: GNewsConfig = field(default_factory=GNewsConfig)
    rss_enabled: bool = True


@dataclass
class DigestConfig:
    default_time: str = "08:00"
    skip_when_empty: bool = False
    # Show the lead article's picture above the digest text.
    lead_image: bool = True


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    database_path: Path = Path("./data/newsbot.db")
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(
                f"{path} not found. Copy config.example.yaml to {path} and edit it."
            )

        # .env sits next to the config file (or the working directory).
        for candidate in (path.parent / ".env", Path(".env")):
            if candidate.exists():
                load_dotenv(candidate)
                break

        raw = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

        tg = raw.get("telegram") or {}
        ai = raw.get("ai") or {}
        anth = ai.get("anthropic") or {}
        olla = ai.get("ollama") or {}
        news = raw.get("news") or {}
        gnews = news.get("gnews") or {}
        rss = news.get("rss") or {}
        dig = raw.get("digest") or {}
        db = raw.get("database") or {}

        cfg = cls(
            telegram=TelegramConfig(
                token=str(tg.get("token") or ""),
                whitelist=[int(x) for x in (tg.get("whitelist") or [])],
                admins=[int(x) for x in (tg.get("admins") or [])],
            ),
            ai=AIConfig(
                backend=str(ai.get("backend") or "anthropic").lower(),
                anthropic=AnthropicConfig(
                    api_key=str(anth.get("api_key") or ""),
                    model=str(anth.get("model") or "claude-haiku-4-5"),
                    max_tokens=int(anth.get("max_tokens") or 600),
                ),
                ollama=OllamaConfig(
                    base_url=str(olla.get("base_url") or "http://localhost:11434"),
                    model=str(olla.get("model") or "llama3.2:3b"),
                    timeout_seconds=int(olla.get("timeout_seconds") or 120),
                ),
            ),
            news=NewsConfig(
                language=str(news.get("language") or "en"),
                country=str(news.get("country") or "us"),
                lookback_hours=int(news.get("lookback_hours") or 24),
                max_articles_per_topic=int(news.get("max_articles_per_topic") or 5),
                max_articles_total=int(news.get("max_articles_total") or 6),
                gnews=GNewsConfig(
                    api_key=str(gnews.get("api_key") or ""),
                    daily_quota=int(gnews.get("daily_quota") or 100),
                ),
                rss_enabled=bool(rss.get("enabled", True)),
            ),
            digest=DigestConfig(
                default_time=str(dig.get("default_time") or "08:00"),
                skip_when_empty=bool(dig.get("skip_when_empty", False)),
                lead_image=bool(dig.get("lead_image", True)),
            ),
            database_path=Path(str(db.get("path") or "./data/newsbot.db")),
            log_level=str(raw.get("log_level") or "INFO").upper(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.telegram.token:
            raise ConfigError(
                "No Telegram token. Set TELEGRAM_BOT_TOKEN in .env "
                "(get one from @BotFather)."
            )
        if self.ai.backend not in ("anthropic", "ollama"):
            raise ConfigError(
                f"ai.backend must be 'anthropic' or 'ollama', got {self.ai.backend!r}"
            )
        if self.ai.backend == "anthropic" and not self.ai.anthropic.api_key:
            raise ConfigError(
                "ai.backend is 'anthropic' but ANTHROPIC_API_KEY is empty. "
                "Set it in .env, or switch ai.backend to 'ollama'."
            )
        if not self.news.gnews.api_key and not self.news.rss_enabled:
            raise ConfigError(
                "No news source: GNews has no API key and RSS is disabled."
            )
        if not self.telegram.whitelist and not self.telegram.admins:
            log.info(
                "No whitelist configured - the first person to message this "
                "bot becomes its owner automatically. Message it now if "
                "that should be you."
            )
