"""Building and sending digests, and answering one-off searches."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from . import formatting, ranking
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

    async def collect(
        self, user: User,
    ) -> tuple[list[str], list[str], int, Article | None]:
        """Returns (rendered blocks, topics with nothing new, count, lead).

        `lead` is the article whose picture heads the message, if any.
        """
        topics = await self.db.list_topics(user.user_id)
        if not topics:
            return [], [], 0, None

        # Fetch deeper than the digest needs: ranking can only pick the best
        # of what it was given, so it wants a pool, not a shortlist.
        pool_size = min(self.cfg.news.max_articles_per_topic * 2, 10)
        results = await asyncio.gather(
            *(self.fetcher.search_unseen(user.user_id, t.query, limit=pool_size)
              for t in topics),
            return_exceptions=True,
        )

        pooled: list[tuple[str, str, list[Article]]] = []
        empty: list[str] = []
        seen_keys: set[str] = set()
        sources: set[str] = set()

        for topic, result in zip(topics, results):
            if isinstance(result, BaseException):
                log.error("Topic %r failed: %s", topic.label, result)
                empty.append(topic.label)
                continue
            articles, source = result
            sources.add(source)
            # A story matching two topics is only worth sending once.
            fresh = [a for a in articles if a.key not in seen_keys]
            seen_keys.update(a.key for a in fresh)
            if fresh:
                pooled.append((topic.label, topic.query, fresh))
            else:
                empty.append(topic.label)

        ranked = ranking.rank(pooled, now=datetime.now(timezone.utc))
        top = ranking.take_top(ranked, self.cfg.news.max_articles_total)
        if not top:
            return [], empty, 0, None

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

        blocks = [formatting.topic_block(label, arts) for label, arts in per_topic]
        lead = _pick_lead(flat)
        # One line that explains any "why no picture?" without guesswork.
        log.info("Digest for %s: %d of %d articles via %s; lead picture from %s",
                 user.user_id, len(flat), len(ranked),
                 "+".join(sorted(sources)) or "none",
                 lead.url if lead else "nothing previewable")
        return blocks, empty, len(flat), lead

    # ------------------------------------------------------------- sending

    async def send_digest(self, bot, user: User, *, manual: bool = False) -> int:
        """Deliver the digest. Returns how many articles were sent."""
        blocks, empty, count, lead = await self.collect(user)

        if not count:
            if manual:
                await self._send(bot, user.user_id, _nothing_new_text(empty))
            elif not self.cfg.digest.skip_when_empty:
                await self._send(bot, user.user_id, _nothing_new_text(empty))
            return 0

        now = datetime.now(ZoneInfo(user.timezone)) if user.timezone else datetime.now()
        # The picture rides on the first message only; if the digest ever has
        # to split, the continuations stay plain.
        preview = lead.url if (lead and self.cfg.digest.lead_image) else ""
        for message in formatting.digest_messages(blocks, when=now,
                                                  empty_topics=empty):
            await self._send(bot, user.user_id, message, preview_url=preview)
            preview = ""
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

    async def _send(self, bot, chat_id: int, text: str, *,
                    preview_url: str = "") -> None:
        # Telegram rejects both preview settings at once, so it is one or the
        # other: a big picture above the text, or no preview at all.
        if preview_url:
            preview = {"link_preview_options": LinkPreviewOptions(
                url=preview_url, prefer_large_media=True, show_above_text=True,
            )}
        else:
            preview = {"disable_web_page_preview": True}
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                **preview,
            )
        except Forbidden:
            log.warning("User %s has blocked the bot; pausing their digest",
                        chat_id)
            await self.db.set_digest_enabled(chat_id, False)
        except TelegramError as exc:
            log.error("Could not message %s: %s", chat_id, exc)


def _pick_lead(articles: list[Article]) -> Article | None:
    """The article whose picture heads the digest.

    Telegram builds the preview from the page's own og:image, so what matters
    is landing on a real publisher URL. A story GNews told us has a picture is
    the safest bet; failing that, any publisher URL is still worth a try,
    since most news pages carry an og:image even when GNews sent none.

    Google News RSS links are `news.google.com` redirects that preview as
    nothing at all, so those are never used - on RSS-only days the digest goes
    out as plain text rather than with an empty grey card on top.
    """
    lead = next((a for a in articles if a.image_url), None)
    if lead is None:
        lead = next((a for a in articles if _is_publisher_url(a.url)), None)
    if lead is None:
        log.info("No previewable article in this digest; sending it without a "
                 "picture (every link is a Google News redirect)")
    return lead


def _is_publisher_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(host) and not host.endswith("news.google.com")


def _nothing_new_text(empty_topics: list[str]) -> str:
    if not empty_topics:
        return (
            "No topics saved yet. Tell me what you care about - "
            "\"i like Manchester United\" or \"follow satellite launches\"."
        )
    names = ", ".join(formatting.esc(t) for t in empty_topics)
    return f"🗞 Nothing new today on: {names}"
