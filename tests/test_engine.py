import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from newsbot.config import Config
from newsbot.db import Database
from newsbot.digest import DigestService
from newsbot.engine import NewsEngine, parse_site_feed
from newsbot.news.models import Article
from tests.test_topics import ScriptedAI

NOW = datetime.now(timezone.utc)


def art(title, source="Reuters", hours=2, url=None):
    return Article(title=title, url=url or f"https://ex.com/{abs(hash(title))}",
                   source=source, published_at=NOW - timedelta(hours=hours))


class FakeRSS:
    def __init__(self, results):
        self.results = results
        self.queries = []

    async def search(self, query, *, limit, lookback_hours):
        self.queries.append(query)
        return list(self.results.get(query, []))


SITE_FEED = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>SpaceNews</title>
<item><title>ESA signs Ariane 6 contract for Galileo L14</title><link>https://spacenews.com/a</link>
<pubDate>{(NOW - timedelta(hours=3)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
<media:content xmlns:media="http://search.yahoo.com/mrss/" url="https://img/x.jpg"/></item>
<item><title>Old story</title><link>https://spacenews.com/old</link>
<pubDate>{(NOW - timedelta(days=9)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
</channel></rss>"""


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        await self.db.ensure_user(7, "john", "John")
        await self.db.add_topic(7, "Space", '"SpaceX" OR "ESA"')
        await self.db.add_topic(7, "Formula 1", '"Formula 1"')
        self.cfg = Config()
        self.cfg.news.engine.feeds = ["https://spacenews.com/feed/"]
        self.rss = FakeRSS({
            '"SpaceX" OR "ESA"': [art("SpaceX Starship reaches orbit on fifth flight"),
                                  art("Starship reaches orbit in fifth test flight", "BBC"),
                                  art("10 best space photos this week", "Listicle Daily")],
            '"Formula 1"': [art("Verstappen wins Singapore Grand Prix")],
        })
        self.engine = NewsEngine(self.db, self.rss, None, self.cfg)
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=SITE_FEED))
        self.engine._client = httpx.AsyncClient(transport=transport)

    async def asyncTearDown(self):
        await self.engine.close()
        self.db.close()
        self.tmp.cleanup()

    def test_site_feed_parsing_keeps_images_and_drops_old(self):
        arts = parse_site_feed(SITE_FEED)
        self.assertEqual([a.title for a in arts], ["ESA signs Ariane 6 contract for Galileo L14"])
        self.assertEqual((arts[0].source, arts[0].image_url), ("SpaceNews", "https://img/x.jpg"))

    async def test_collect_searches_query_and_label_and_reads_feeds(self):
        added = await self.engine.collect()
        self.assertEqual(added, 5)
        self.assertIn('"Formula 1"', self.rss.queries)
        self.assertIn("Space", self.rss.queries)           # the label, as a second search
        self.assertEqual(await self.engine.collect(), 0)    # nothing new the second time

    async def test_ai_scores_rank_and_merge_duplicate_coverage(self):
        await self.engine.collect()
        # Batch order is newest-first; answer by title so the test doesn't care.
        titles = [r[0] for r in await self.engine._read("SELECT title FROM pool ORDER BY first_seen DESC")]
        verdict = {"SpaceX Starship reaches orbit on fifth flight": ("Space", 9, 9),
                   "Starship reaches orbit in fifth test flight": ("Space", 9, 9),
                   "10 best space photos this week": ("Space", 4, 1),
                   "Verstappen wins Singapore Grand Prix": ("Formula 1", 9, 7),
                   "ESA signs Ariane 6 contract for Galileo L14": ("Space", 8, 7)}
        self.engine.ai = ScriptedAI([{"i": i, "topic": verdict[t][0], "relevance": verdict[t][1],
                                      "impact": verdict[t][2], "why": "x"}
                                     for i, t in enumerate(await self._batch_titles(), 1)])
        self.assertEqual(await self.engine.score_user(7), 5)
        ranked = await self.engine.ranked(7)
        self.assertEqual(ranked[0]["outlets"], 2)            # two outlets, one story
        self.assertEqual(ranked[0]["topic"], "Space")
        self.assertNotIn("10 best space photos this week", [r["title"] for r in ranked])
        picked = await self.engine.pick(7, total=8, per_topic=1)
        self.assertEqual(sorted(p["topic"] for p in picked), ["Formula 1", "Space"])

    async def _batch_titles(self):
        # score_user builds the batch from the newest pool rows; mirror it.
        rows = await self.engine._read("SELECT title FROM pool ORDER BY first_seen DESC")
        return [r[0] for r in rows]

    async def test_without_a_model_keywords_stand_in(self):
        await self.engine.collect()
        await self.engine.score_user(7)
        ranked = await self.engine.ranked(7, min_relevance=1)
        self.assertTrue(any(r["topic"] == "Formula 1" for r in ranked))

    async def test_a_thumbs_down_removes_the_story(self):
        await self.engine.collect()
        await self.engine.score_user(7)
        before = await self.engine.ranked(7, min_relevance=1)
        await self.engine.vote(7, before[0]["key"], -1)
        after = await self.engine.ranked(7, min_relevance=1)
        self.assertNotIn(before[0]["key"], [a["key"] for a in after])
        prompt_taste = await self.engine._taste(7)
        self.assertIn("NOT for them", prompt_taste)

    async def test_digest_uses_the_engine_and_marks_whole_story_sent(self):
        await self.engine.collect()
        await self.engine.score_user(7)
        digest = DigestService(self.db, None, None, self.cfg)
        digest.engine = self.engine
        user = await self.db.get_user(7)
        blocks, empty, count, lead = await digest.collect(user)
        self.assertGreater(count, 0)
        items = digest.last_items[7]
        self.assertTrue(all("key" in i and "why" in i for i in items))
        # Everything just sent is gone from the next briefing.
        self.assertEqual(await self.engine.pick(7, 8), [])

    async def test_breaking_news_is_capped_and_sent_once(self):
        await self.engine.collect()
        self.engine.ai = ScriptedAI([{"i": i, "topic": "Space", "relevance": 9, "impact": 10, "why": "x"}
                                     for i in range(1, 6)])
        await self.engine.score_user(7)

        class App:
            sent = []

            async def breaking_news(self, uid, story, bot):
                self.sent.append(story["title"])
        self.engine.app = App()
        self.cfg.news.engine.breaking_per_day = 2
        self.assertEqual(await self.engine.breaking(7), 2)
        self.assertEqual(await self.engine.breaking(7), 0)


if __name__ == "__main__":
    unittest.main()
