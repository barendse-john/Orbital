"""The news engine: collect widely every hour, judge carefully, deliver by score.

Why this exists: the old digest ran one keyword search per topic, once a
day, and kept whatever ~10 results came back. A big story worded differently
from the query never reached the bot, which is exactly what John saw - the
news was online, the bot just never looked at it.

Now, every hour:

1. **Collect** into a shared pool (SQLite): Google News RSS for every topic's
   query and its plain label, over 48 h, plus the site feeds in
   `news.engine.feeds`. RSS only - free and unlimited, so the GNews quota is
   left for one-off searches in chat.
2. **Score** each new pool article per user with the AI, in batches, against
   a rubric: which of their topics it belongs to, relevance 0-10, impact
   0-10, and a few words of why. The user's 👍/👎 from the app are shown to
   the model as examples of their taste. Without a model, keyword matching
   stands in (relevance) and every story is "notable" (impact 5).
3. **Deliver** by score. The final score blends relevance, impact, how many
   outlets carry the story, source credibility, freshness and the user's
   votes for that outlet. The morning briefing takes the best unsent
   stories (at most 3 per topic); anything scored as major breaks through
   as a notification straight away, capped per day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from . import ranking
from .ai.base import AIError, describe
from .brain import extract_json
from .news.models import Article

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool (
    key          TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    source       TEXT,
    published_at TEXT,
    description  TEXT,
    image_url    TEXT,
    origin       TEXT,
    first_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pool_seen ON pool(first_seen);

CREATE TABLE IF NOT EXISTS pool_scores (
    user_id   INTEGER NOT NULL,
    key       TEXT NOT NULL,
    topic     TEXT,
    relevance INTEGER NOT NULL,
    impact    INTEGER NOT NULL,
    why       TEXT,
    scored_at TEXT NOT NULL,
    alerted   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS news_votes (
    user_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    vote    INTEGER NOT NULL,
    title   TEXT,
    source  TEXT,
    at      TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

SCORE_SYSTEM = """You are the news editor for one reader. You judge headlines for them.

Their interests (label: what they mean by it):
{interests}
{taste}
For EACH numbered headline, return a JSON array of objects:
{{"i": <number>, "topic": "<exact label from the list, or null>", "relevance": 0-10, "impact": 0-10, "why": "<max 12 words>"}}

relevance - how squarely it is about one of their interests AS THEY DESCRIBED IT
  (a passing mention or a loose keyword match is 0-3).
impact - how much actually happened:
  9-10 major event: launch success/failure, crash, breakthrough, big deal or
       contract, election result, major policy change, record, death of a key figure
  6-8  real news worth knowing
  3-5  routine update, minor announcement, incremental step
  0-2  opinion, preview, listicle, promo, how-to, stock-price chatter, rehash
Be strict: most headlines are not important. Return only the JSON array."""

TIER_VALUE = {"trusted": 1.0, "academic": 0.8, "neutral": 0.5, "low": 0.0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_site_feed(content: bytes | str, lookback_hours: int = 48) -> list[Article]:
    """Any RSS/Atom site feed -> Articles (Google News has its own parser)."""
    feed = feedparser.parse(content)
    site = (feed.feed.get("title") or "").strip() if getattr(feed, "feed", None) else ""
    cutoff = _now() - timedelta(hours=lookback_hours)
    out = []
    for entry in feed.entries[:60]:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published = None
        if parsed:
            try:
                published = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        if published and published < cutoff:
            continue
        image = ""
        for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
            if isinstance(media, dict) and str(media.get("url", "")).startswith("http"):
                image = media["url"]
                break
        if not image:
            for enc in entry.get("enclosures") or []:
                if str(enc.get("type", "")).startswith("image") and enc.get("href"):
                    image = enc["href"]
                    break
        article = Article(title=entry.get("title", ""), url=entry.get("link", ""),
                          source=site, published_at=published,
                          description=entry.get("summary", ""), image_url=image)
        if article.title and article.url:
            out.append(article)
    return out


class NewsEngine:
    def __init__(self, db, rss, ai, cfg, app=None):
        self.db = db
        self.rss = rss                       # GoogleNewsRSS (may be None)
        self.ai = ai
        self.cfg = cfg                       # the whole Config
        self.ecfg = cfg.news.engine
        self.app = app
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        with db._lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()

    # --------------------------------------------------------- db helpers

    async def _read(self, sql, params=()):
        return await asyncio.to_thread(self.db._read, sql, params)

    async def _write(self, sql, params=()):
        return await asyncio.to_thread(self.db._write, sql, params)

    async def _many(self, sql, rows):
        def run():
            with self.db._lock:
                self.db.conn.executemany(sql, rows)
                self.db.conn.commit()
        await asyncio.to_thread(run)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0, connect=15.0), follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (newsbot; personal reader)"})
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ tick

    async def tick(self) -> dict:
        """One hourly pass. Serialised: a slow hour can't overlap the next."""
        if self._lock.locked():
            return {"skipped": True}
        async with self._lock:
            added = await self.collect()
            scored = 0
            owner = await self.db.owner_id()
            if owner is not None and await self.db.list_topics(owner):
                scored += await self.score_user(owner)
                await self.breaking(owner)
            await self._write("DELETE FROM pool WHERE first_seen < ?",
                              (_iso(_now() - timedelta(days=7)),))
            await self._write("DELETE FROM pool_scores WHERE scored_at < ?",
                              (_iso(_now() - timedelta(days=7)),))
            log.info("News engine: %d new in pool, %d scored", added, scored)
            return {"added": added, "scored": scored}

    # --------------------------------------------------------- collect

    async def collect(self) -> int:
        searches: dict[str, str] = {}
        owner = await self.db.owner_id()
        for t in (await self.db.list_topics(owner) if owner is not None else []):
            searches.setdefault(t.query, t.query)
            label = t.label.strip()
            if label and not re.search(r"\b%s\b" % re.escape(label.lower()), t.query.lower()):
                searches.setdefault(f'"{label}"' if " " in label else label, t.query)
        found: list[tuple[Article, str]] = []
        if self.rss is not None:
            for q in searches:
                try:
                    arts = await self.rss.search(q, limit=self.ecfg.per_search,
                                                 lookback_hours=self.ecfg.lookback_hours)
                    found += [(a, f"search:{q}") for a in arts]
                except Exception as exc:  # noqa: BLE001 - one bad query must not stop the hour
                    log.warning("Pool search %r failed: %s", q, describe(exc))
                await asyncio.sleep(1.0)   # be polite to Google
        for url in self.ecfg.feeds:
            try:
                resp = await self.client.get(url)
                resp.raise_for_status()
                arts = await asyncio.to_thread(parse_site_feed, resp.content,
                                               self.ecfg.lookback_hours)
                found += [(a, f"feed:{url}") for a in arts]
            except Exception as exc:  # noqa: BLE001
                log.warning("Feed %s failed: %s", url, describe(exc))
        rows = [(a.key, a.url, a.title, a.source, _iso(a.published_at), a.description[:600],
                 a.image_url, origin, _iso(_now())) for a, origin in found if a.key]
        before = (await self._read("SELECT COUNT(*) FROM pool"))[0][0]
        await self._many("INSERT OR IGNORE INTO pool (key, url, title, source, published_at, "
                         "description, image_url, origin, first_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                         rows)
        after = (await self._read("SELECT COUNT(*) FROM pool"))[0][0]
        return after - before

    # ----------------------------------------------------------- score

    async def _taste(self, user_id: int) -> str:
        rows = await self._read(
            "SELECT vote, title FROM news_votes WHERE user_id = ? AND vote != 0 "
            "ORDER BY at DESC LIMIT 30", (user_id,))
        liked = [r[1] for r in rows if r[0] > 0][:12]
        disliked = [r[1] for r in rows if r[0] < 0][:12]
        out = ""
        if liked:
            out += "\nHeadlines they marked as GOOD picks:\n" + "\n".join(f"+ {t}" for t in liked) + "\n"
        if disliked:
            out += "\nHeadlines they marked as NOT for them:\n" + "\n".join(f"- {t}" for t in disliked) + "\n"
        return out

    async def score_user(self, user_id: int) -> int:
        topics = await self.db.list_topics(user_id)
        if not topics:
            return 0
        # Which topic a search belonged to, by query and by label.
        by_search = {t.query: t for t in topics}
        by_search.update({t.label: t for t in topics})
        rows = await self._read(
            "SELECT p.key, p.title, p.source, p.description, p.origin FROM pool p "
            "LEFT JOIN pool_scores s ON s.key = p.key AND s.user_id = ? "
            "WHERE s.key IS NULL AND p.first_seen >= ? ORDER BY p.first_seen DESC LIMIT ?",
            (user_id, _iso(_now() - timedelta(hours=self.ecfg.lookback_hours)),
             self.ecfg.max_score_per_tick * 3))
        # Only spend the model on plausible candidates: anything a search for
        # one of this user's topics found, any site-feed item, and other
        # users' search results that at least mention one of their topics.
        cands = []
        for key, title, source, desc, origin in rows:
            art = Article(title=title, url="", source=source or "", description=desc or "")
            found_by = None
            if origin.startswith("search:"):
                found_by = by_search.get(origin[7:]) or by_search.get(origin[7:].strip('"'))
            if found_by or origin.startswith("feed:") or any(
                    ranking.topic_match(art, t.query) > 0.3 for t in topics):
                cands.append((key, title, source or "", desc or "", found_by))
        cands = cands[: self.ecfg.max_score_per_tick]
        if not cands:
            return 0

        results: dict[str, tuple] = {}
        if self.ai is not None:
            interests = "\n".join(f"- {t.label}: {t.query}" for t in topics)
            system = SCORE_SYSTEM.format(interests=interests, taste=await self._taste(user_id))
            for start in range(0, len(cands), 30):
                batch = cands[start:start + 30]
                prompt = "\n".join(f"{i}. {c[1]} ({c[2]})" for i, c in enumerate(batch, 1))
                try:
                    raw = await self.ai.complete(system, prompt, max_tokens=60 * len(batch) + 100)
                except AIError as exc:
                    log.warning("Scoring failed, keyword fallback for this batch: %s", exc)
                    continue
                data = extract_json(raw)
                if not isinstance(data, list):
                    log.warning("Unparseable scores: %s", (raw or "")[:200])
                    continue
                labels = {t.label.lower(): t.label for t in topics}
                for item in data:
                    try:
                        i = int(item.get("i")) - 1
                        key = batch[i][0]
                    except (TypeError, ValueError, IndexError, AttributeError):
                        continue
                    topic = labels.get(str(item.get("topic") or "").lower())
                    rel = max(0, min(10, int(item.get("relevance") or 0))) if topic else 0
                    imp = max(0, min(10, int(item.get("impact") or 0)))
                    results[key] = (topic, rel, imp, str(item.get("why") or "")[:120])

        # Fallback for anything the model didn't (or couldn't) score: the
        # topic whose search found it (the search engine already judged it
        # relevant), else keyword overlap.
        for key, title, source, desc, found_by in cands:
            if key in results:
                continue
            art = Article(title=title, url="", source=source, description=desc)
            best = max(topics, key=lambda t: ranking.topic_match(art, t.query))
            match = ranking.topic_match(art, best.query)
            if found_by is not None:
                results[key] = (found_by.label, max(6, round(match * 8)), 5, "")
            else:
                results[key] = (best.label if match > 0 else None, round(match * 8), 5, "")

        now = _iso(_now())
        await self._many(
            "INSERT OR REPLACE INTO pool_scores (user_id, key, topic, relevance, impact, why, "
            "scored_at, alerted) VALUES (?,?,?,?,?,?,?,0)",
            [(user_id, k, t, r, i, w, now) for k, (t, r, i, w) in results.items()])
        return len(results)

    # ------------------------------------------------------------ rank

    async def ranked(self, user_id: int, *, hours: int = 36, min_relevance: int = 5,
                     unsent_only: bool = False) -> list[dict]:
        """Scored, de-duplicated stories for one user, best first."""
        rows = await self._read(
            "SELECT p.key, p.url, p.title, p.source, p.published_at, p.description, p.image_url, "
            "s.topic, s.relevance, s.impact, s.why, p.first_seen, v.vote "
            "FROM pool_scores s JOIN pool p ON p.key = s.key "
            "LEFT JOIN news_votes v ON v.user_id = s.user_id AND v.key = s.key "
            "WHERE s.user_id = ? AND s.relevance >= ? AND s.topic IS NOT NULL "
            "AND COALESCE(p.published_at, p.first_seen) >= ?",
            (user_id, min_relevance, _iso(_now() - timedelta(hours=hours))))
        if not rows:
            return []
        source_bias = await self._source_bias(user_id)
        arts, meta = [], {}
        for (key, url, title, source, pub, desc, img, topic, rel, imp, why, seen, vote) in rows:
            a = Article(title=title, url=url, source=source or "", published_at=_parse_dt(pub),
                        description=desc or "", image_url=img or "", key=key)
            arts.append(a)
            meta[key] = {"topic": topic, "relevance": rel, "impact": imp, "why": why or "",
                         "vote": vote or 0}
        if unsent_only:
            unseen = await self.db.filter_unseen(user_id, [a.key for a in arts])
            arts = [a for a in arts if a.key in unseen]
        out = []
        now = _now()
        for cluster in ranking.cluster_articles(arts):
            # The member the model rated highest decides the story's worth;
            # the best-sourced member is the one we link to.
            best = max(cluster.members, key=lambda a: (meta[a.key]["relevance"] + meta[a.key]["impact"]))
            m = meta[best.key]
            if any(meta[a.key]["vote"] < 0 for a in cluster.members):
                continue                                  # they said no to this story
            outlets = len({(a.source or "").lower() for a in cluster.members})
            lead = cluster.lead
            score = (0.45 * m["relevance"] / 10 + 0.30 * m["impact"] / 10
                     + 0.10 * min(1.0, (outlets - 1) / 4)
                     + 0.07 * TIER_VALUE[ranking.source_tier(lead)]
                     + (0.08 * ranking.recency(best, now) if best.published_at else 0.04))
            score += source_bias.get((lead.source or "").lower(), 0.0)
            out.append({"key": lead.key, "url": lead.url, "title": lead.title,
                        "source": lead.source, "image": lead.image_url or best.image_url,
                        "published": _iso(lead.published_at) or "", "summary": lead.description[:280],
                        "topic": m["topic"], "relevance": m["relevance"], "impact": m["impact"],
                        "why": m["why"], "outlets": outlets, "score": round(score, 3),
                        "vote": m["vote"], "members": [a.key for a in cluster.members],
                        "_article": lead})
        out.sort(key=lambda d: d["score"], reverse=True)
        return out

    async def _source_bias(self, user_id: int) -> dict[str, float]:
        rows = await self._read(
            "SELECT LOWER(source), SUM(vote), COUNT(*) FROM news_votes "
            "WHERE user_id = ? AND vote != 0 GROUP BY LOWER(source)", (user_id,))
        return {src: 0.12 * total / (n + 2) for src, total, n in rows if src}

    async def pick(self, user_id: int, total: int, per_topic: int = 3) -> list[dict]:
        """The briefing: best unsent stories, a few per topic."""
        picked, counts = [], {}
        for story in await self.ranked(user_id, unsent_only=True):
            if counts.get(story["topic"], 0) >= per_topic:
                continue
            counts[story["topic"]] = counts.get(story["topic"], 0) + 1
            picked.append(story)
            if len(picked) >= total:
                break
        return picked

    # -------------------------------------------------------- breaking

    async def breaking(self, user_id: int) -> int:
        if not self.ecfg.breaking or self.app is None:
            return 0
        today = await self._read(
            "SELECT COUNT(*) FROM pool_scores WHERE user_id = ? AND alerted = 1 AND scored_at >= ?",
            (user_id, _iso(_now() - timedelta(hours=24))))
        room = self.ecfg.breaking_per_day - today[0][0]
        if room <= 0:
            return 0
        sent = 0
        for story in await self.ranked(user_id, hours=6, min_relevance=8, unsent_only=True):
            if story["impact"] < 9 or sent >= room:
                continue
            flagged = await self._read(
                "SELECT 1 FROM pool_scores WHERE user_id = ? AND key IN (%s) AND alerted = 1"
                % ",".join("?" * len(story["members"])), (user_id, *story["members"]))
            if flagged:
                continue
            await self._many("UPDATE pool_scores SET alerted = 1 WHERE user_id = ? AND key = ?",
                             [(user_id, k) for k in story["members"]])
            await self.db.mark_sent(user_id, [(story["key"], story["url"])])
            await self.app.breaking_news(user_id, story)
            sent += 1
        return sent

    # -------------------------------------------------------- feedback

    async def vote(self, user_id: int, key: str, vote: int) -> None:
        vote = max(-1, min(1, int(vote)))
        row = await self._read("SELECT title, source FROM pool WHERE key = ?", (key,))
        title, source = (row[0][0], row[0][1]) if row else ("", "")
        if not row:
            b = await self._read("SELECT items FROM briefings WHERE user_id = ? ORDER BY id DESC LIMIT 14",
                                 (user_id,))
            for (items,) in b:
                for it in json.loads(items):
                    if it.get("key") == key:
                        title, source = it.get("title", ""), it.get("source", "")
        await self._write(
            "INSERT OR REPLACE INTO news_votes (user_id, key, vote, title, source, at) "
            "VALUES (?,?,?,?,?,?)", (user_id, key, vote, title, source, _iso(_now())))


def story_item(story: dict, summary: str = "") -> dict:
    """A ranked story as stored in a briefing / sent to the app."""
    return {k: story[k] for k in ("key", "topic", "title", "url", "source", "published",
                                  "image", "why", "outlets", "score", "vote")} | {
        "summary": summary or story.get("summary", "")}
