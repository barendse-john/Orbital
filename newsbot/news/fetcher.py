"""Picks a source, applies the caps, and never sends the same story twice.

Order of preference: GNews while the daily allowance lasts, then Google News
RSS. If GNews errors for any other reason the request is handed back to the
quota counter and RSS answers instead, so a bad API day never costs a digest.

Note the two GNews limits are different things: 403 means the day's 100
requests are gone and RSS takes over until midnight UTC, while 429 only means
too many requests in one second - it costs nothing but that one query.
"""

from __future__ import annotations

import logging

from ..config import NewsConfig
from ..db import Database
from .gnews import (GNewsClient, GNewsError, GNewsQuotaExceeded,
                    GNewsRateLimited)
from .models import Article
from .rss import GoogleNewsRSS, RSSError

log = logging.getLogger(__name__)


class NewsFetcher:
    def __init__(self, cfg: NewsConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.gnews = GNewsClient(
            cfg.gnews.api_key, language=cfg.language, country=cfg.country,
            requests_per_second=cfg.gnews.requests_per_second,
        )
        self.rss = (
            GoogleNewsRSS(language=cfg.language, country=cfg.country)
            if cfg.rss_enabled else None
        )

    # ------------------------------------------------------------------

    async def search(
        self, query: str, *, limit: int | None = None,
        lookback_hours: int | None = None,
    ) -> tuple[list[Article], str]:
        """Returns (articles, source_used)."""
        limit = limit or self.cfg.max_articles_per_topic
        lookback = lookback_hours or self.cfg.lookback_hours

        if self.gnews.enabled:
            claimed = await self.db.claim_gnews_call(self.cfg.gnews.daily_quota)
            if claimed:
                try:
                    found = await self.gnews.search(
                        query, limit=limit, lookback_hours=lookback
                    )
                    return _dedupe(found)[:limit], "gnews"
                except GNewsQuotaExceeded as exc:
                    log.warning("%s - switching to RSS for the rest of today", exc)
                    await self.db.exhaust_gnews_today(self.cfg.gnews.daily_quota)
                except GNewsRateLimited as exc:
                    # Only a burst, never the daily allowance: give the
                    # request back and let RSS answer this one query.
                    log.warning("%s - RSS answers this one", exc)
                    await self.db.release_gnews_call()
                except GNewsError as exc:
                    log.warning("GNews failed (%s) - falling back to RSS", exc)
                    await self.db.release_gnews_call()
            else:
                log.info("GNews daily quota spent - using RSS")

        if self.rss is None:
            return [], "none"

        try:
            found = await self.rss.search(
                query, limit=limit, lookback_hours=lookback
            )
            return _dedupe(found)[:limit], "rss"
        except RSSError as exc:
            log.error("RSS failed for %r: %s", query, exc)
            return [], "none"

    async def widening_search(
        self, query: str, *, limit: int | None = None,
    ) -> tuple[list[Article], str, str]:
        """Look further back until something turns up.

        A 24h window is right for a digest and wrong for a question: "tell me
        about King Oyo of Uganda" has no reporting today and plenty in the
        last month. Returns (articles, source, how far back it had to go).
        Each widening costs another search, so this is only for questions
        someone asked, never for the digest.
        """
        base = self.cfg.lookback_hours
        windows = [(base, f"{base}h"), (24 * 7, "week"), (24 * 30, "month")]
        for hours, label in windows:
            articles, source = await self.search(
                query, limit=limit, lookback_hours=hours
            )
            if articles:
                return articles, source, label
        return [], "none", windows[-1][1]

    async def search_unseen(
        self, user_id: int, query: str, *, limit: int | None = None,
    ) -> tuple[list[Article], str]:
        """Like search(), minus anything this user has already been sent."""
        limit = limit or self.cfg.max_articles_per_topic
        # Ask for extra so filtering out old stories still fills the digest.
        articles, source = await self.search(query, limit=min(limit * 2, 10))
        if not articles:
            return [], source
        unseen_keys = await self.db.filter_unseen(
            user_id, [a.key for a in articles]
        )
        fresh = [a for a in articles if a.key in unseen_keys]
        return fresh[:limit], source

    async def close(self) -> None:
        await self.gnews.close()
        if self.rss is not None:
            await self.rss.close()


def _dedupe(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    out: list[Article] = []
    for a in articles:
        if a.key in seen:
            continue
        seen.add(a.key)
        out.append(a)
    return out
