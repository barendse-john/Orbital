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
    # Free plan allows 1/s; paid plans 10/s.
    requests_per_second: float = 1.0


DEFAULT_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://spacenews.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    # New York Times (John subscribes, so the links open in full for him)
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Space.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
]


@dataclass
class EngineConfig:
    enabled: bool = True
    # Site feeds read every hour on top of the per-topic searches. The AI
    # decides which of your topics (if any) each story belongs to.
    feeds: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDS))
    lookback_hours: int = 48
    per_search: int = 25
    max_score_per_tick: int = 120
    collect_every_minutes: int = 60
    briefing_size: int = 8
    per_topic: int = 3
    breaking: bool = True
    breaking_per_day: int = 3


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
    engine: EngineConfig = field(default_factory=EngineConfig)


@dataclass
class DigestConfig:
    default_time: str = "08:00"
    skip_when_empty: bool = False
    # Show the lead article's picture above the digest text.
    lead_image: bool = True


@dataclass
class SpaceConfig:
    enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # What Telegram messages link to. Empty = http://<pi hostname>.local:<port>
    public_url: str = ""
    remind_before_minutes: list[int] = field(default_factory=lambda: [1440, 30])
    launch_refresh_minutes: int = 20
    satellite_groups: list[str] = field(
        default_factory=lambda: ["stations", "visual", "gnss", "weather"])
    satellite_refresh_hours: int = 6
    max_satellites_per_group: int = 2000


@dataclass
class ReportsConfig:
    # Markdown reports (the Kalulu morning briefing) from a Google Drive
    # folder, read in the Orbital app. Off by itself until a folder id is set.
    enabled: bool = True
    folder_id: str = ""
    service_account_file: str = "./data/google-service-account.json"
    # Telegram user ids who may read them in the app and get notified.
    # Empty = the bot's owner (not every admin).
    chat_ids: list[int] = field(default_factory=list)
    poll_minutes: int = 15
    # Older than this when first seen = listed in the app without a
    # notification, so a fresh install doesn't ring the phone for old ones.
    max_age_hours: int = 36
    archive_dir: str = ""       # empty = <database folder>/reports


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    space: SpaceConfig = field(default_factory=SpaceConfig)
    reports: ReportsConfig = field(default_factory=ReportsConfig)
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
        sp = raw.get("space") or {}
        sp_defaults = SpaceConfig()

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
                    requests_per_second=float(
                        gnews.get("requests_per_second") or 1.0),
                ),
                rss_enabled=bool(rss.get("enabled", True)),
                engine=_engine_cfg(news.get("engine") or {}),
            ),
            digest=DigestConfig(
                default_time=str(dig.get("default_time") or "08:00"),
                skip_when_empty=bool(dig.get("skip_when_empty", False)),
                lead_image=bool(dig.get("lead_image", True)),
            ),
            space=SpaceConfig(
                enabled=bool(sp.get("enabled", True)),
                web_host=str(sp.get("web_host") or sp_defaults.web_host),
                web_port=int(sp.get("web_port") or sp_defaults.web_port),
                public_url=str(sp.get("public_url") or "").rstrip("/"),
                remind_before_minutes=[int(m) for m in (
                    sp.get("remind_before_minutes")
                    or sp_defaults.remind_before_minutes)],
                launch_refresh_minutes=max(5, int(
                    sp.get("launch_refresh_minutes")
                    or sp_defaults.launch_refresh_minutes)),
                satellite_groups=[str(g) for g in (
                    sp.get("satellite_groups") or sp_defaults.satellite_groups)],
                satellite_refresh_hours=max(2, int(
                    sp.get("satellite_refresh_hours")
                    or sp_defaults.satellite_refresh_hours)),
                max_satellites_per_group=int(
                    sp.get("max_satellites_per_group")
                    or sp_defaults.max_satellites_per_group),
            ),
            reports=_reports_cfg(raw.get("reports") or {}),
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


def _reports_cfg(raw: dict) -> ReportsConfig:
    """The folder and key can come from .env alone (the names the standalone
    briefing_relay.py used), so no config.yaml section is needed."""
    d = ReportsConfig()
    return ReportsConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        folder_id=str(raw.get("folder_id")
                      or os.environ.get("GDRIVE_REPORTS_FOLDER_ID") or "").strip(),
        service_account_file=str(raw.get("service_account_file")
                                 or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
                                 or d.service_account_file),
        chat_ids=[int(x) for x in (raw.get("chat_ids") or [])],
        poll_minutes=max(5, int(raw.get("poll_minutes") or d.poll_minutes)),
        max_age_hours=int(raw.get("max_age_hours") or d.max_age_hours),
        archive_dir=str(raw.get("archive_dir") or ""),
    )


def _engine_cfg(raw: dict) -> EngineConfig:
    d = EngineConfig()
    feeds = raw.get("feeds")
    return EngineConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        feeds=[str(f) for f in feeds] if isinstance(feeds, list) else d.feeds,
        lookback_hours=int(raw.get("lookback_hours") or d.lookback_hours),
        per_search=int(raw.get("per_search") or d.per_search),
        max_score_per_tick=int(raw.get("max_score_per_tick") or d.max_score_per_tick),
        collect_every_minutes=max(15, int(raw.get("collect_every_minutes") or d.collect_every_minutes)),
        briefing_size=int(raw.get("briefing_size") or d.briefing_size),
        per_topic=int(raw.get("per_topic") or d.per_topic),
        breaking=bool(raw.get("breaking", d.breaking)),
        breaking_per_day=int(raw.get("breaking_per_day") or d.breaking_per_day),
    )
