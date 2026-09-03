"""Building and sending digests, and answering one-off searches."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from . import formatting
from .brain import summarise_articles
from .db import Database, User
from .news import Article, NewsFetcher

log = logging.getLogger(__name__)


class DigestService:
    def __init__(self, db: Database, fetcher: NewsFetcher, ai, cfg):
        self.db = db
        self.fetcher = fetcher
        self.ai = ai
        self.cfg = cfg

    # ------------------------------------------------------------ building

    async def collect(self, user: User) -> tuple[list[str], list[str], int]:
        """Returns (rendered blocks, topics with nothing new, article count)."""
        topics = await self.db.list_topics(user.user_id)
        if not topics:
            return [], [], 0

        cap = self.cfg.news.max_articles_per_topic
        results = await asyncio.gather(
            *(self.fetcher.search_unseen(user.user_id, t.query, limit=cap)
              for t in topics),
            return_exceptions=True,
        )

        per_topic: list[tuple[str, list[Article]]] = []
        empty: list[str] = []
        seen_keys: set[str] = set()

        for topic, result in zip(topics, results):
            if isinstance(result, BaseException):
                log.error("Topic %r failed: %s", topic.label, result)
                empty.append(topic.label)
                continue
            articles, _source = result
            # A story matching two topics is only worth sending once.
            fresh = [a for a in articles if a.key not in seen_keys]
            seen_keys.update(a.key for a in fresh)
            if fresh:
                per_topic.append((topic.label, fresh))
            else:
                empty.append(topic.label)

        flat = [a for _, arts in per_topic for a in arts]
        if not flat:
            return [], empty, 0

        summaries = await summarise_articles(self.ai, flat)
        for article, summary in zip(flat, summaries):
            article.summary = summary

        await self.db.mark_sent(
            user.user_id, [(a.key, a.url) for a in flat]
        )

        blocks = [formatting.topic_block(label, arts) for label, arts in per_topic]
        return blocks, empty, len(flat)

    # ------------------------------------------------------------- sending

    async def send_digest(self, bot, user: User, *, manual: bool = False) -> int:
        """Deliver the digest. Returns how many articles were sent."""
        blocks, empty, count = await self.collect(user)

        if not count:
            if manual:
                await self._send(bot, user.user_id, _nothing_new_text(empty))
            elif not self.cfg.digest.skip_when_empty:
                await self._send(bot, user.user_id, _nothing_new_text(empty))
            return 0

        now = datetime.now(ZoneInfo(user.timezone)) if user.timezone else datetime.now()
        for message in formatting.digest_messages(blocks, when=now,
                                                  empty_topics=empty):
            await self._send(bot, user.user_id, message)
        log.info("Sent %d articles to %s", count, user.user_id)
        return count

    async def search_reply(self, user_id: int, query: str, limit: int = 5) -> str:
        """A one-off search, formatted as a single message body."""
        articles, source = await self.fetcher.search(query, limit=limit)
        if not articles:
            return (
                f"Nothing in the last {self.cfg.news.lookback_hours}h for "
                f"<b>{formatting.esc(query)}</b>. Try different wording?"
            )

        summaries = await summarise_articles(self.ai, articles)
        for article, summary in zip(articles, summaries):
            article.summary = summary

        await self.db.mark_sent(user_id, [(a.key, a.url) for a in articles])

        header = f"🔎 <b>{formatting.esc(query)}</b>"
        body = "\n".join(formatting.article_line(a) for a in articles)
        if source == "rss":
            body += "\n\n<i>via Google News RSS</i>"
        return f"{header}\n{body}"

    async def _send(self, bot, chat_id: int, text: str) -> None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Forbidden:
            log.warning("User %s has blocked the bot; pausing their digest",
                        chat_id)
            await self.db.set_digest_enabled(chat_id, False)
        except TelegramError as exc:
            log.error("Could not message %s: %s", chat_id, exc)


def _nothing_new_text(empty_topics: list[str]) -> str:
    if not empty_topics:
        return (
            "No topics saved yet. Tell me what you care about - "
            "\"i like Manchester United\" or \"follow satellite launches\"."
        )
    names = ", ".join(formatting.esc(t) for t in empty_topics)
    return f"🗞 Nothing new today on: {names}"
