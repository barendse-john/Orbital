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

    async def complete(self, system, user, *, max_tokens=600,
                       temperature=0.0, attempts=None):
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
        self.kwargs = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        self.kwargs.append(kwargs)


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
        self.assertIn("https://a/1", body)
        self.assertIn("Summary 1.", body)

    async def test_the_cap_keeps_the_best_story_of_each_topic(self):
        self.cfg.news.max_articles_total = 2
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 2)
        self.assertEqual(len(bot.messages), 1)
        body = bot.messages[0][1]
        self.assertIn("Manchester United", body)
        self.assertIn("Rockets", body)
        # BBC outranks Sky, so the weaker United story is the one dropped.
        self.assertIn("https://a/1", body)
        self.assertNotIn("https://a/2", body)

    async def test_articles_cut_by_the_cap_come_back_next_time(self):
        self.cfg.news.max_articles_total = 2
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        second = await self.digest.send_digest(bot, self.user)
        self.assertEqual(second, 1)
        self.assertIn("https://a/2", bot.messages[-1][1])

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
        self.assertEqual(bot.messages[0][1].count("https://a/1"), 1)

    async def test_digest_still_goes_out_when_the_model_is_down(self):
        self.digest.ai = StubAI(fail=True)
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 3)
        self.assertIn("United sign a striker", bot.messages[0][1])  # no summary

    async def test_search_reply_marks_results_as_seen(self):
        body = await self.digest.search_reply(7, "United")
        self.assertIn("https://a/1", body)
        bot = StubBot()
        sent = await self.digest.send_digest(bot, self.user)
        self.assertEqual(sent, 1)  # only the rocket story is left

    async def test_lead_picture_heads_the_message(self):
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        opts = bot.kwargs[0].get("link_preview_options")
        self.assertIsNotNone(opts)
        # First article with a known picture wins, not simply the first article.
        self.assertEqual(opts.url, "https://a/2")
        self.assertTrue(opts.show_above_text)
        self.assertNotIn("disable_web_page_preview", bot.kwargs[0])

    async def test_a_publisher_url_is_tried_even_with_no_known_picture(self):
        # GNews sometimes sends no image; the page usually still has an
        # og:image, so a real publisher link is worth previewing.
        self.fetcher.rss.articles = [
            Article(title="United draw at home", url="https://sky.com/2",
                    source="Sky"),
        ]
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        self.assertEqual(
            bot.kwargs[0]["link_preview_options"].url, "https://sky.com/2"
        )

    async def test_google_news_redirects_are_never_previewed(self):
        self.fetcher.rss.articles = [
            Article(title="United draw at home", source="Sky",
                    url="https://news.google.com/rss/articles/CBMiabc"),
        ]
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        self.assertNotIn("link_preview_options", bot.kwargs[0])
        self.assertTrue(bot.kwargs[0]["disable_web_page_preview"])

    async def test_lead_picture_can_be_switched_off(self):
        self.cfg.digest.lead_image = False
        bot = StubBot()
        await self.digest.send_digest(bot, self.user)
        self.assertNotIn("link_preview_options", bot.kwargs[0])

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


class WideningSearchTests(unittest.IsolatedAsyncioTestCase):
    """A question is not a digest: look further back before giving up."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(news=NewsConfig(gnews=GNewsConfig(api_key="")),
                          digest=DigestConfig())
        self.fetcher = NewsFetcher(self.cfg.news, self.db)
        self.windows = []

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    def _source(self, found_at_hours):
        windows = self.windows

        class Source:
            api_key = ""
            enabled = False

            async def search(inner, query, *, limit=5, lookback_hours=24):
                windows.append(lookback_hours)
                if lookback_hours < found_at_hours:
                    return []
                return [Article(title="King Oyo turns 34", url="https://a/1",
                                source="Daily Monitor")]

            async def close(inner):
                pass

        return Source()

    async def test_it_stops_as_soon_as_something_turns_up(self):
        self.fetcher.rss = self._source(0)
        articles, _source, window = await self.fetcher.widening_search("king oyo")
        self.assertTrue(articles)
        self.assertEqual(window, "24h")
        self.assertEqual(self.windows, [24])  # no wasted searches

    async def test_it_reaches_back_a_month_when_today_has_nothing(self):
        self.fetcher.rss = self._source(24 * 30)
        articles, _source, window = await self.fetcher.widening_search("king oyo")
        self.assertTrue(articles)
        self.assertEqual(window, "month")
        self.assertEqual(self.windows, [24, 168, 720])

    async def test_a_month_of_nothing_is_reported_as_nothing(self):
        self.fetcher.rss = self._source(99999)
        articles, source, _window = await self.fetcher.widening_search("king oyo")
        self.assertEqual(articles, [])
        self.assertEqual(source, "none")


class BackgroundFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(news=NewsConfig(gnews=GNewsConfig(api_key="")),
                          digest=DigestConfig())
        self.fetcher = NewsFetcher(self.cfg.news, self.db)
        self.fetcher.rss = StubSource([])

    async def asyncTearDown(self):
        await self.fetcher.close()
        self.db.close()
        self.tmp.cleanup()

    async def test_no_reporting_still_gets_an_answer(self):
        class Knowing(StubAI):
            async def complete(inner, system, user, *, max_tokens=600,
                               temperature=0.0, attempts=None):
                return ("King Oyo Nyimba Kabamba Iguru Rukidi IV has reigned "
                        "over Toro since 1995, crowned at three.")

        digest = DigestService(self.db, self.fetcher, Knowing(), self.cfg)
        body = await digest.search_reply(7, "King Oyo Uganda")
        self.assertIn("reigned", body)
        # Framed as background, never passed off as reporting.
        self.assertIn("may be out of date", body)

    async def test_it_says_so_when_it_knows_nothing_either(self):
        digest = DigestService(self.db, self.fetcher, StubAI(fail=True),
                               self.cfg)
        body = await digest.search_reply(7, "King Oyo Uganda")
        self.assertIn("don't have much on it", body)
