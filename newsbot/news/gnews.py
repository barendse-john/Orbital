"""GNews API client (gnews.io). Free tier: 100 requests a day."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .models import Article

log = logging.getLogger(__name__)

SEARCH_URL = "https://gnews.io/api/v4/search"


class GNewsError(RuntimeError):
    """The API call failed."""


class GNewsQuotaExceeded(GNewsError):
    """Daily request allowance is spent - fall back to RSS."""


class GNewsClient:
    def __init__(self, api_key: str, language: str = "en", country: str = "us"):
        self.api_key = api_key
        self.language = language
        self.country = country
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))

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

        try:
            resp = await self._client.get(SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            raise GNewsError(f"GNews request failed: {exc}") from exc

        if resp.status_code in (403, 429):
            raise GNewsQuotaExceeded(
                f"GNews quota or plan limit reached ({resp.status_code})"
            )
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
