"""Asking for a digest opens a conversation, and the conversation ends."""

import json
import tempfile
import unittest
from pathlib import Path

from newsbot.brain import ChatTurn, is_goodbye, link_citations, plan_chat_turn
from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig, TelegramConfig
from newsbot.db import Database
from newsbot.handlers import BotHandlers
from newsbot.news.models import Article

from tests.test_topics import FakeContext, FakeUpdate, ScriptedAI


def art(title, source, url):
    return Article(title=title, url=url, source=source, description=title)


ARTICLES = [
    art("Nvidia confirms $13bn Hugging Face deal", "Reuters", "https://r.com/1"),
    art("Analysts call the deal defensive", "Bloomberg", "https://b.com/2"),
]


class FakeFetcher:
    def __init__(self, articles):
        self.articles = articles
        self.queries = []

    async def search(self, query, *, limit=5, lookback_hours=None):
        self.queries.append(query)
        return self.articles[:limit], "gnews"


class GoodbyeTests(unittest.TestCase):
    def test_closing_words_are_recognised_without_a_model(self):
        for text in ("thanks", "ok thanks that's all", "bye", "never mind",
                     "cheers", "nah I'm done"):
            self.assertTrue(is_goodbye(text), text)

    def test_a_question_is_not_a_goodbye(self):
        for text in ("what about Nvidia?", "thanks to whom?", "done deal?"):
            self.assertFalse(is_goodbye(text), text)


class CitationTests(unittest.TestCase):
    def test_markers_become_links_to_the_article_they_number(self):
        out = link_citations("Nvidia bought it [1]. Analysts disagreed [2].",
                             ARTICLES)
        self.assertIn('<a href="https://r.com/1">Reuters</a>', out)
        self.assertIn('<a href="https://b.com/2">Bloomberg</a>', out)

    def test_a_number_with_no_article_behind_it_is_dropped(self):
        # The model writes numbers, never URLs, so a bad citation can only
        # ever vanish - it cannot point somewhere that does not exist.
        self.assertEqual(link_citations("Something happened [7].", ARTICLES),
                         "Something happened.")


class PlanTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_question_becomes_a_search(self):
        turn = await plan_chat_turn(
            ScriptedAI({"search": "nvidia hugging face", "reply": None,
                        "end": False}), [], "what's happening with nvidia")
        self.assertEqual(turn.search, "nvidia hugging face")
        self.assertFalse(turn.end)

    async def test_goodbye_never_reaches_the_model(self):
        ai = ScriptedAI({"search": "x"})
        turn = await plan_chat_turn(ai, [], "thanks, that's all")
        self.assertTrue(turn.end)
        self.assertEqual(ai.prompts, [])

    async def test_with_no_model_the_message_is_the_search(self):
        turn = await plan_chat_turn(ScriptedAI({}, fail=True), [], "nvidia news")
        self.assertEqual(turn.search, "nvidia news")


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(
            telegram=TelegramConfig(token="t", whitelist=[7]),
            news=NewsConfig(gnews=GNewsConfig(api_key="")),
            digest=DigestConfig(default_time="08:00"),
        )
        self.fetcher = FakeFetcher(ARTICLES)
        self.ai = ScriptedAI({})
        self.bot = BotHandlers(self.cfg, self.db, self.ai, self.fetcher,
                               digest=None, scheduler=None)
        await self.db.ensure_user(7, "john", "John")
        await self.db.set_timezone(7, "Europe/Amsterdam")

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def _say(self, text):
        update = FakeUpdate(7, text)
        await self.bot.on_text(update, FakeContext())
        return update.effective_message.replies

    async def test_asking_for_a_digest_starts_a_conversation_instead(self):
        self.ai.replies = [
            {"action": "digest_now", "reply": "ok"},
            {"search": "nvidia", "reply": None, "end": False},
            json.dumps(["Nvidia bought it.", "Analysts disagreed."]),
            "Nvidia bought Hugging Face for $13bn [1]. Analysts called it "
            "defensive [2].",
        ]
        opener = await self._say("send me a digest")
        self.assertIn("What do you want to know about?", opener[0])
        self.assertIsNotNone(await self.db.get_chat(7))

        answered = await self._say("what's happening with nvidia")
        self.assertIn('<a href="https://r.com/1">Reuters</a>', answered[0])
        self.assertIn("$13bn", answered[0])
        self.assertEqual(self.fetcher.queries, ["nvidia"])
        # The conversation remembers what was said.
        self.assertEqual(len(await self.db.get_chat(7)), 2)

    async def test_a_goodbye_closes_it(self):
        await self.db.start_chat(7)
        replies = await self._say("thanks, that's all")
        self.assertEqual(replies, ["Alright."])
        self.assertIsNone(await self.db.get_chat(7))

    async def test_a_command_closes_it_rather_than_being_answered(self):
        await self.db.start_chat(7)
        self.ai.replies = [{"action": "list_topics"}]
        await self._say("/topics")
        self.assertIsNone(await self.db.get_chat(7))
        self.assertEqual(self.fetcher.queries, [])

    async def test_going_quiet_closes_it(self):
        await self.db.start_chat(7)
        self.assertIsNone(await self.db.get_chat(7, idle_minutes=0))

    async def test_chat_results_stay_available_for_the_morning_digest(self):
        self.ai.replies = [
            {"search": "nvidia", "reply": None, "end": False},
            json.dumps(["Nvidia bought it.", "Analysts disagreed."]),
            "Nvidia bought Hugging Face [1].",
        ]
        await self.db.start_chat(7)
        await self._say("what about nvidia")
        # Nothing was marked sent - a story worth discussing now is still
        # worth putting in tomorrow's digest.
        unseen = await self.db.filter_unseen(7, [a.key for a in ARTICLES])
        self.assertEqual(len(unseen), 2)


if __name__ == "__main__":
    unittest.main()
