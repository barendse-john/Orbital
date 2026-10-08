"""End-to-end briefing assembly with a stub model and a stub app."""

import json
import tempfile
import unittest
from pathlib import Path

from newsbot.ai.base import AIBackend, AIError
from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig
from newsbot.db import Database
from newsbot.digest import DigestService
from newsbot.news.fetcher import NewsFetcher
from newsbot.news.models import Article


class StubAI(AIBackend):
    """Answers summary prompts with one line per article."""

    name = "stub"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def complete(self, system, user, *, max_tokens=600,
                       temperature=0.0, attempts=None):
        self.calls += 1
        if self.fail:
            raise AIError("backend down")
        if "one-line news summaries" in system:
            count = sum(1 for line in user.splitlines()
                        if line.strip()[:1].isdigit() and "." in line)
            return json.dumps([f"Summary {i + 1}." for i in range(count)])
        return "[]"


class StubApp:
    """Stands in for AppService: records every briefing handed to the app."""

    def __init__(self):
        self.briefings = []

    async def on_briefing(self, user_id, items):
        self.briefings.append((user_id, items))


class StubSource:
    def __init__(self, articles):
        self.articles = articles
        self.api_key = ""

    @property
    def enabled(self):
        return False

    async def search(self, query, *, limit=5, lookback_hours=24):
        return [a for a in self.articles if query.lower() in a.title.lower()][:limit]

    async def close(self):
        pass


class DigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()

        self.cfg = Config(
            news=NewsConfig(gnews=GNewsConfig(api_key=""), max_articles_per_topic=5),
            digest=DigestConfig(default_time="08:00"),
        )
        self.fetcher = NewsFetcher(self.cfg.news, self.db)
        self.fetcher.rss = StubSource([
            Article(title="United sign a striker", url="https://a/1", source="BBC"),
            Article(title="United draw at home", url="https://a/2", source="Sky",
                    image_url="https://img/2.jpg"),
            Article(title="Rocket launch delayed", url="https://b/1", source="NYT",
                    image_url="https://img/3.jpg"),
        ])
        self.ai = StubAI()
        self.digest = DigestService(self.db, self.fetcher, self.ai, self.cfg)
        self.app = StubApp()
        self.digest.app = self.app

        self.user = await self.db.ensure_user(7, "john", "John")
        await self.db.set_timezone(7, "Europe/Amsterdam")
        await self.db.add_topic(7, "Manchester United", "United")
        await self.db.add_topic(7, "Rockets", "Rocket")
        self.user = await self.db.get_user(7)

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    def urls(self, n=-1):
        return [i["url"] for i in self.app.briefings[n][1]]

    async def test_the_briefing_covers_every_topic(self):
        sent = await self.digest.send_digest(self.user)
        self.assertEqual(sent, 3)
        self.assertEqual(len(self.app.briefings), 1)
        user_id, items = self.app.briefings[0]
        self.assertEqual(user_id, 7)
        self.assertEqual({i["topic"] for i in items}, {"Manchester United", "Rockets"})
        self.assertIn("https://a/1", self.urls())
        self.assertIn("Summary 1.", [i["summary"] for i in items])

    async def test_the_cap_keeps_the_best_story_of_each_topic(self):
        self.cfg.news.max_articles_total = 2
        sent = await self.digest.send_digest(self.user)
        self.assertEqual(sent, 2)
        self.assertEqual({i["topic"] for i in self.app.briefings[0][1]},
                         {"Manchester United", "Rockets"})
        # BBC outranks Sky, so the weaker United story is the one dropped.
        self.assertIn("https://a/1", self.urls())
        self.assertNotIn("https://a/2", self.urls())

    async def test_articles_cut_by_the_cap_come_back_next_time(self):
        self.cfg.news.max_articles_total = 2
        await self.digest.send_digest(self.user)
        second = await self.digest.send_digest(self.user)
        self.assertEqual(second, 1)
        self.assertEqual(self.urls(), ["https://a/2"])

    async def test_articles_are_never_sent_twice(self):
        await self.digest.send_digest(self.user)
        second = await self.digest.send_digest(self.user)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.app.briefings), 1)   # nothing new, no push

    async def test_a_story_matching_two_topics_appears_once(self):
        await self.db.add_topic(7, "Strikers", "United sign")
        sent = await self.digest.send_digest(await self.db.get_user(7))
        self.assertEqual(sent, 3)
        self.assertEqual(self.urls().count("https://a/1"), 1)

    async def test_the_briefing_still_goes_out_when_the_model_is_down(self):
        self.digest.ai = StubAI(fail=True)
        sent = await self.digest.send_digest(self.user)
        self.assertEqual(sent, 3)
        self.assertIn("United sign a striker",
                      [i["title"] for i in self.app.briefings[0][1]])

    async def test_pictures_are_passed_to_the_app(self):
        await self.digest.send_digest(self.user)
        images = {i["url"]: i["image"] for i in self.app.briefings[0][1]}
        self.assertEqual(images["https://a/2"], "https://img/2.jpg")

    async def test_no_topics_means_no_briefing(self):
        for topic in await self.db.list_topics(7):
            await self.db.remove_topic(7, topic.label)
        self.assertEqual(await self.digest.send_digest(self.user), 0)
        self.assertEqual(self.app.briefings, [])


if __name__ == "__main__":
    unittest.main()
