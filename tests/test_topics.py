"""Narrowing a vague topic: the question, the answer, and the query it makes."""

import json
import tempfile
import unittest
from pathlib import Path

from newsbot.ai.base import AIBackend, AIError
from newsbot.brain import TopicPlan, plain_query, plan_topic, query_is_sane, quote
from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig, TelegramConfig
from newsbot.db import Database
from newsbot.handlers import BotHandlers, _is_skip


class ScriptedAI(AIBackend):
    """Replies with whatever JSON the test queued, in order."""

    name = "scripted"

    def __init__(self, *replies, fail=False):
        self.replies = list(replies)
        self.prompts = []
        self.fail = fail

    async def complete(self, system, user, *, max_tokens=600, temperature=0.0):
        self.prompts.append(user)
        if self.fail:
            raise AIError("backend down")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, str) else json.dumps(reply)


BROAD = {"question": "Markets and central banks, company earnings, or crypto?",
         "label": "Finance", "query": None}
NARROWED = {"question": None, "label": "Markets and Rates",
            "query": '"financial markets" OR "central bank" OR "interest rates"'}


class PlanTopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_broad_topic_gets_one_question(self):
        plan = await plan_topic(ScriptedAI(BROAD), "finance")
        self.assertIsNotNone(plan.question)
        self.assertIsNone(plan.query)
        # The label stays the user's own word until they have answered.
        self.assertEqual(plan.label, "finance")

    async def test_a_specific_topic_is_saved_straight_away(self):
        plan = await plan_topic(ScriptedAI(
            {"question": None, "label": "Manchester United",
             "query": '"Manchester United"'}), "manchester united")
        self.assertIsNone(plan.question)
        self.assertEqual(plan.query, '"Manchester United"')

    async def test_the_answer_produces_the_query(self):
        ai = ScriptedAI(NARROWED)
        plan = await plan_topic(ai, "finance", transcript=[
            {"q": BROAD["question"], "a": "markets and rates"},
        ])
        self.assertEqual(plan.label, "Markets and Rates")
        self.assertIn("central bank", plan.query)
        self.assertIn("markets and rates", ai.prompts[0])

    async def test_it_can_ask_again_while_the_answer_is_still_broad(self):
        plan = await plan_topic(ScriptedAI(BROAD), "finance", transcript=[
            {"q": "which bit?", "a": "all of it really"},
        ])
        self.assertIsNotNone(plan.question)

    async def test_it_stops_asking_after_four_rounds(self):
        # Even if the model would keep going, adding a topic is not an
        # interrogation: round five must produce a query.
        transcript = [{"q": f"q{i}", "a": f"a{i}"} for i in range(4)]
        plan = await plan_topic(ScriptedAI(BROAD), "finance",
                                transcript=transcript)
        self.assertIsNone(plan.question)
        self.assertTrue(plan.query)

    async def test_a_malformed_query_is_thrown_away(self):
        plan = await plan_topic(ScriptedAI(
            {"question": None, "label": "Finance",
             "query": '"unclosed OR (broken'}), "finance")
        self.assertTrue(query_is_sane(plan.query))

    async def test_no_model_still_gives_a_usable_query(self):
        plan = await plan_topic(ScriptedAI(BROAD, fail=True),
                                "manchester united")
        self.assertIsNone(plan.question)
        self.assertEqual(plan.query, '"manchester united"')


class PlainQueryTests(unittest.TestCase):
    def test_multi_word_phrases_are_quoted(self):
        self.assertEqual(quote("Manchester United"), '"Manchester United"')
        self.assertEqual(quote("finance"), "finance")

    def test_the_answer_narrows_the_fallback_too(self):
        self.assertEqual(
            plain_query("finance", "markets and central banks"),
            "finance AND (markets OR central OR banks)",
        )

    def test_words_after_a_negation_are_not_searched_for(self):
        self.assertNotIn("crypto", plain_query("finance", "markets, not crypto"))

    def test_skip_answers_are_recognised(self):
        for text in ("skip", "Skip!", "whatever", "no preference"):
            self.assertTrue(_is_skip(text), text)
        self.assertFalse(_is_skip("markets and rates"))


# --------------------------------------------------------------------------
# The Telegram round trip, with just enough of PTB faked to drive it.
# --------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id, text=""):
        self.effective_user = type("U", (), {
            "id": user_id, "username": "john", "first_name": "John"})()
        self.effective_message = FakeMessage(text)


class FakeBot:
    async def send_chat_action(self, *a, **kw):
        pass

    async def send_message(self, *a, **kw):
        pass


class FakeContext:
    def __init__(self, args=None):
        self.bot = FakeBot()
        self.user_data = {}
        self.args = args or []


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
        self.ai = ScriptedAI(BROAD, NARROWED)
        self.bot = BotHandlers(self.cfg, self.db, self.ai, fetcher=None,
                               digest=None, scheduler=None)
        await self.db.ensure_user(7, "john", "John")
        await self.db.set_timezone(7, "Europe/Amsterdam")

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def _say(self, text, context=None):
        update = FakeUpdate(7, text)
        await self.bot.on_text(update, context or FakeContext())
        return update.effective_message.replies

    async def test_vague_topic_is_questioned_then_saved_from_the_answer(self):
        self.ai.replies = [
            {"action": "add_topic", "topic": "finance", "query": "finance",
             "reply": "Following finance."},
            BROAD,
            NARROWED,
        ]
        asked = await self._say("i want finance news")
        self.assertIn("central banks", asked[0])
        self.assertEqual(await self.db.list_topics(7), [])  # nothing saved yet

        saved = await self._say("markets and rates")
        topics = await self.db.list_topics(7)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].label, "Markets and Rates")
        self.assertIn("central bank", topics[0].query)
        self.assertIn("searching:", saved[0])

    async def test_skip_saves_the_topic_as_stated(self):
        self.ai.replies = [
            {"action": "add_topic", "topic": "finance", "query": "finance",
             "reply": "Following finance."},
            BROAD,
        ]
        await self._say("i want finance news")
        await self._say("skip")
        topics = await self.db.list_topics(7)
        self.assertEqual(topics[0].label, "finance")
        self.assertEqual(topics[0].query, "finance")

    async def test_a_command_abandons_the_question(self):
        self.ai.replies = [
            {"action": "add_topic", "topic": "finance", "query": "finance",
             "reply": "ok"},
            BROAD,
        ]
        await self._say("i want finance news")
        self.assertIsNotNone(await self.db.get_pending_topic(7))
        await self._say("/topics")
        # The question is dropped rather than swallowing an unrelated command.
        self.assertIsNone(await self.db.get_pending_topic(7))
        self.assertEqual(await self.db.list_topics(7), [])

    async def test_retune_rewrites_an_existing_topic_without_renaming_it(self):
        await self.db.add_topic(7, "finance", "finance")
        self.ai.replies = [BROAD, NARROWED]
        update = FakeUpdate(7)
        await self.bot.cmd_retune(update, FakeContext(args=["finance"]))
        self.assertIn("central banks", update.effective_message.replies[0])

        await self._say("markets and rates")
        topics = await self.db.list_topics(7)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].label, "finance")  # their word, kept
        self.assertIn("central bank", topics[0].query)

    async def test_a_stale_question_is_forgotten(self):
        await self.db.set_pending_topic(7, "finance", "which bit?")
        self.assertIsNone(await self.db.get_pending_topic(7, max_age_minutes=0))


if __name__ == "__main__":
    unittest.main()
