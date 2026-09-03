"""End-to-end digest assembly with a stub model and a stub Telegram bot."""

import json
import tempfile
import unittest
from pathlib import Path

from newsbot.ai.base import AIBackend, AIError
from newsbot.brain import parse_intent
from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig
from newsbot.db import Database
from newsbot.digest import DigestService
from newsbot.news.fetcher import NewsFetcher
from newsbot.news.models import Article


class StubAI(AIBackend):
    """Answers intent prompts with JSON and summary prompts with a list."""

    name = "stub"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def complete(self, system, user, *, max_tokens=600, temperature=0.0):
        self.calls += 1
        if self.fail:
            raise AIError("backend down")
        if "one-line news summaries" in system:
            count = sum(1 for line in user.splitlines()
                        if line.strip()[:1].isdigit() and "." in line)
            return json.dumps([f"Summary {i + 1}." for i in range(count)])
        return json.dumps({
            "action": "add_topic", "topic": "Manchester United",
            "query": "Manchester United", "reply": "Following Manchester United.",
        })


class StubBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


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
            Article(title="United draw at home", url="https://a/2", source="Sky"),
            Article(title="Rocket launch delayed", url="https://b/1", source="NYT"),
        ])
        self.ai = StubAI()
        self.digest = DigestService(self.db, self.fetcher, self.ai, self.cfg)

        self.user = await self.db.ensure_user(7, "john", "John")
        await self.db.set_timezone(7, "Europe/Amsterdam")
        await self.db.add_topic(7, "Manchester United", "United")
        await self.db.add_topic(7, "Rockets", "Rocket")
        self.user = await self.db.get_user(7)

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_digest_is_one_message_covering_every_topic(self):
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 3)
        self.assertEqual(len(bot.messages), 1)
        body = bot.messages[0][1]
        self.assertIn("Manchester United", body)
        self.assertIn("Rockets", body)
        self.assertIn("United sign a striker", body)
        self.assertIn("Summary 1.", body)

    async def test_articles_are_never_sent_twice(self):
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        second = await self.digest.send_digest(bot, self.user)
        self.assertEqual(second, 0)
        self.assertIn("Nothing new today", bot.messages[-1][1])

    async def test_a_story_matching_two_topics_appears_once(self):
        await self.db.add_topic(7, "Strikers", "United sign")
        bot = StubBot()
        sent = await self.digest.send_digest(bot, await self.db.get_user(7))
        self.assertEqual(sent, 3)
        self.assertEqual(bot.messages[0][1].count("United sign a striker"), 1)

    async def test_digest_still_goes_out_when_the_model_is_down(self):
        self.digest.ai = StubAI(fail=True)
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 3)
        self.assertIn("United sign a striker", bot.messages[0][1])

    async def test_search_reply_marks_results_as_seen(self):
        body = await self.digest.search_reply(7, "United")
        self.assertIn("United sign a striker", body)
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 1)  # only the rocket story is left

    async def test_no_topics_prompts_the_user(self):
        await self.db.clear_topics(7)
        bot = StubBot()
        await self.digest.send_digest(bot, self.user, manual=True)
        self.assertIn("No topics saved yet", bot.messages[0][1])


class IntentWithModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_json_is_used(self):
        intent = await parse_intent(StubAI(), "i like Manchester United")
        self.assertEqual(intent.action, "add_topic")
        self.assertEqual(intent.topic, "Manchester United")
        self.assertEqual(intent.reply, "Following Manchester United.")

    async def test_keyword_rules_take_over_when_the_model_fails(self):
        intent = await parse_intent(StubAI(fail=True), "follow Arsenal")
        self.assertEqual(intent.action, "add_topic")
        self.assertEqual(intent.topic, "Arsenal")


if __name__ == "__main__":
    unittest.main()
