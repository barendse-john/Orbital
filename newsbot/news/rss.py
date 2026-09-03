"""Google News RSS: no key, no quota, slightly messier data."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .models import Article

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"

# Google News locale codes, derived from the configured language/country.
_CEID = {"en-us": ("en-US", "US"), "en-gb": ("en-GB", "GB"),
         "nl-nl": ("nl", "NL"), "de-de": ("de", "DE"), "fr-fr": ("fr", "FR")}


class RSSError(RuntimeError):
    """The feed could not be fetched or parsed."""


class GoogleNewsRSS:
    def __init__(self, language: str = "en", country: str = "us"):
        self.language = language.lower()
        self.country = country.lower()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        )

    def _locale(self) -> tuple[str, str]:
        return _CEID.get(f"{self.language}-{self.country}",
                         (self.language, self.country.upper()))

    async def search(
        self, query: str, *, limit: int = 5, lookback_hours: int = 24,
    ) -> list[Article]:
        hl, gl = self._locale()
        window = max(1, round(lookback_hours))
        params = {
            "q": f"{query} when:{window}h",
            "hl": hl,
            "gl": gl,
            "ceid": f"{gl}:{hl.split('-')[0]}",
        }

        try:
            resp = await self._client.get(RSS_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RSSError(f"Google News RSS failed: {exc}") from exc

        return await asyncio.to_thread(
            parse_feed, resp.content, limit, lookback_hours
        )

    async def close(self) -> None:
        await self._client.aclose()


def parse_feed(
    content: bytes | str, limit: int = 5, lookback_hours: int = 24,
) -> list[Article]:
    """Google News RSS -> Articles. Separated out so it can be tested offline."""
    feed = feedparser.parse(content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours + 2)

    articles: list[Article] = []
    for entry in feed.entries:
        title, source = _split_title(entry.get("title", ""))
        published = _entry_dt(entry)
        if published and published < cutoff:
            continue
        article = Article(
            title=title,
            url=entry.get("link", ""),
            source=source or _entry_source(entry),
            published_at=published,
            description=entry.get("summary", ""),
        )
        # Google's <description> is usually just the headline wrapped in a
        # link, which makes a useless summary - drop it when that's all it is.
        if _echoes_title(article.description, article.title):
            article.description = ""
        if not (article.title and article.url):
            continue
        articles.append(article)
        if len(articles) >= limit:
            break
    return articles


def _echoes_title(description: str, title: str) -> bool:
    squash = lambda text: re.sub(r"[^a-z0-9]+", "", text.lower())  # noqa: E731
    desc, head = squash(description), squash(title)
    return not desc or (head and desc.startswith(head[:60]))


def _split_title(raw: str) -> tuple[str, str]:
    """Google formats titles as 'Headline - Publisher'.

    Headlines contain dashes too, so the tail only counts as a publisher when
    it looks like one: short, few words, and leaving a real headline behind.
    """
    raw = raw.strip()
    if " - " not in raw:
        return raw, ""
    head, _, tail = raw.rpartition(" - ")
    head, tail = head.strip(), tail.strip()
    looks_like_publisher = (
        len(tail) <= 40 and len(tail.split()) <= 5 and len(head) >= 8
    )
    return (head, tail) if looks_like_publisher else (raw, "")


def _entry_source(entry) -> str:
    src = entry.get("source")
    if isinstance(src, dict):
        return src.get("title", "")
    return getattr(src, "title", "") if src else ""


def _entry_dt(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
