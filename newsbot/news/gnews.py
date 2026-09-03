"""GNews API client (gnews.io). Free tier: 100 requests a day."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .models import Article

log = logging.getLogger(__name__)

SEARCH_URL = "https://gnews.io/api/v4/search"


class GNewsError(RuntimeError):
    """The API call failed."""


class GNewsQuotaExceeded(GNewsError):
    """Daily request allowance is spent - fall back to RSS."""


class GNewsRateLimited(GNewsError):
    """Too many requests per second. Says nothing about the daily allowance."""


class GNewsClient:
    def __init__(self, api_key: str, language: str = "en", country: str = "us",
                 requests_per_second: float = 1.0):
        self.api_key = api_key
        self.language = language
        self.country = country
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        # The free plan allows one request a second, and a digest searches
        # every topic at once - so starts have to be spaced or the whole
        # batch comes back 429.
        self._min_gap = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._gate = asyncio.Lock()
        self._next_slot = 0.0

    async def _wait_turn(self) -> None:
        """Hold each request back until its slot in the per-second budget."""
        if not self._min_gap:
            return
        async with self._gate:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = time.monotonic() + self._min_gap

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(
        self, query: str, *, limit: int = 5, lookback_hours: int = 24,
    ) -> list[Article]:
        if not self.api_key:
            raise GNewsError("No GNews API key configured")

        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        params = {
            "q": query,
            "lang": self.language,
            "country": self.country,
            "max": str(max(1, min(limit, 10))),
            "sortby": "publishedAt",
            "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "apikey": self.api_key,
        }

        # One retry: a 429 is usually a burst that a moment's wait clears.
        for attempt in (1, 2):
            await self._wait_turn()
            try:
                resp = await self._client.get(SEARCH_URL, params=params)
            except httpx.HTTPError as exc:
                raise GNewsError(f"GNews request failed: {exc}") from exc
            if resp.status_code != 429 or attempt == 2:
                break
            log.debug("GNews rate limit hit on %r; retrying once", query)

        # 403 is the daily allowance; 429 is only the per-second rate limit,
        # so the two must not be confused - one costs the rest of the day.
        if resp.status_code == 403:
            raise GNewsQuotaExceeded("GNews daily quota spent (403)")
        if resp.status_code == 429:
            raise GNewsRateLimited("GNews rate limit (429): too many per second")
        if resp.status_code >= 400:
            raise GNewsError(f"GNews error {resp.status_code}: {resp.text[:200]}")

        articles = []
        for item in resp.json().get("articles", []):
            articles.append(
                Article(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    source=(item.get("source") or {}).get("name", ""),
                    published_at=_parse_dt(item.get("publishedAt")),
                    description=item.get("description") or "",
                    image_url=item.get("image") or "",
                )
            )
        return [a for a in articles if a.title and a.url]

    async def close(self) -> None:
        await self._client.aclose()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
