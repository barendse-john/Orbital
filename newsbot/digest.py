"""Building the daily briefing and delivering it to the Orbital app."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import ranking
from .engine import story_item
from .brain import summarise_articles
from .db import Database, User
from .news import Article, NewsFetcher

log = logging.getLogger(__name__)


class DigestService:
    def __init__(self, db: Database, fetcher: NewsFetcher, ai, cfg):
        self.app = None          # AppService, attached in __main__
        self.engine = None       # NewsEngine, attached in __main__
        self.db = db
        self.fetcher = fetcher
        self.ai = ai
        self.cfg = cfg

    # ------------------------------------------------------------ building

    async def collect(self, user: User) -> list[dict]:
        """The briefing's stories, as the app shows them, marked as sent.

        Empty when there are no topics or nothing new turned up.
        """
        topics = await self.db.list_topics(user.user_id)
        if not topics:
            return []

        if self.engine is not None:
            ecfg = self.cfg.news.engine
            stories = await self.engine.pick(user.user_id, ecfg.briefing_size, ecfg.per_topic)
            if stories:
                return await self._finish_engine(user, stories)
            # Nothing scored yet (the first hour after an install): fall
            # through to the classic per-topic search so the digest still goes.

        # Fetch deeper than the digest needs: ranking can only pick the best
        # of what it was given, so it wants a pool, not a shortlist.
        pool_size = min(self.cfg.news.max_articles_per_topic * 2, 10)
        results = await asyncio.gather(
            *(self.fetcher.search_unseen(user.user_id, t.query, limit=pool_size)
              for t in topics),
            return_exceptions=True,
        )

        pooled: list[tuple[str, str, list[Article]]] = []
        seen_keys: set[str] = set()
        sources: set[str] = set()

        for topic, result in zip(topics, results):
            if isinstance(result, BaseException):
                log.error("Topic %r failed: %s", topic.label, result)
                continue
            articles, source = result
            sources.add(source)
            # A story matching two topics is only worth sending once.
            fresh = [a for a in articles if a.key not in seen_keys]
            seen_keys.update(a.key for a in fresh)
            if fresh:
                pooled.append((topic.label, topic.query, fresh))

        ranked = ranking.rank(pooled, now=datetime.now(timezone.utc))
        top = ranking.take_top(ranked, self.cfg.news.max_articles_total)
        if not top:
            return []

        per_topic: list[tuple[str, list[Article]]] = []
        for label, scored in top:
            for existing_label, arts in per_topic:
                if existing_label == label:
                    arts.append(scored.article)
                    break
            else:
                per_topic.append((label, [scored.article]))

        if log.isEnabledFor(logging.DEBUG):
            for label, scored in top:
                log.debug("  %5.2f %dx [%s] %s", scored.score, scored.sources,
                          label, scored.article.title[:70])

        flat = [a for _, arts in per_topic for a in arts]

        summaries = await summarise_articles(self.ai, flat)
        for article, summary in zip(flat, summaries):
            article.summary = summary

        await self.db.mark_sent(
            user.user_id, [(a.key, a.url) for a in flat]
        )

        log.info("Briefing for %s: %d of %d articles via %s", user.user_id,
                 len(flat), len(ranked), "+".join(sorted(sources)) or "none")
        return [
            {"key": a.key, "topic": label, "title": a.title, "url": a.url, "source": a.source,
             "summary": a.summary or a.description,
             "published": a.published_at.isoformat() if a.published_at else "",
             "image": a.image_url}
            for label, arts in per_topic for a in arts]

    async def _finish_engine(self, user: User, stories: list[dict]) -> list[dict]:
        per_topic: list[tuple[str, list[Article]]] = []
        for story in stories:
            art = story["_article"]
            for label, arts in per_topic:
                if label == story["topic"]:
                    arts.append(art)
                    break
            else:
                per_topic.append((story["topic"], [art]))
        flat = [a for _, arts in per_topic for a in arts]
        summaries = await summarise_articles(self.ai, flat)
        for article, summary in zip(flat, summaries):
            article.summary = summary
        by_key = {s["key"]: s for s in stories}
        # Every telling of a sent story counts as sent, so tomorrow doesn't
        # bring the same event back from a different outlet.
        await self.db.mark_sent(user.user_id, [
            (k, by_key[a.key]["url"]) for a in flat for k in by_key[a.key]["members"]])
        log.info("Briefing for %s: %d stories from the news engine",
                 user.user_id, len(flat))
        return [story_item(by_key[a.key], a.summary) for a in flat]

    # ------------------------------------------------------------- sending

    async def send_digest(self, user: User) -> int:
        """Build the briefing, save it for the app's News tab and announce it
        by push. Returns how many stories it holds."""
        items = await self.collect(user)
        if items and self.app is not None:
            await self.app.on_briefing(user.user_id, items)
        log.info("Briefing for %s: %d stories", user.user_id, len(items))
        return len(items)
