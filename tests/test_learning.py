"""Topics get better from being talked about, not from filling in a form."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.brain import fallback_intent, refine_query
from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig, TelegramConfig
from newsbot.db import Database
from newsbot.handlers import BotHandlers

from tests.test_chat import ARTICLES, FakeFetcher
from tests.test_topics import FakeContext, FakeUpdate, ScriptedAI

REFINED = {"query": '"satellite launch" OR "reusable rockets"',
           "note": "added reusable rockets"}


class RefineTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_steer_rewrites_the_query(self):
        out = await refine_query(ScriptedAI(REFINED), "Satellites and Space",
                                 '"satellite launch"', "add reusable rockets")
        self.assertEqual(out[0], '"satellite launch" OR "reusable rockets"')
        self.assertEqual(out[1], "added reusable rockets")

    async def test_a_broken_query_is_refused(self):
        self.assertIsNone(await refine_query(
            ScriptedAI({"query": '"unclosed OR (', "note": "x"}),
            "Space", '"satellite launch"', "add rockets"))

    async def test_an_unchanged_query_is_not_announced(self):
        self.assertIsNone(await refine_query(
            ScriptedAI({"query": '"satellite launch"', "note": "no change"}),
            "Space", '"satellite launch"', "add rockets"))


class RetunePhrasingTests(unittest.TestCase):
    def test_complaining_about_a_topic_starts_a_retune(self):
        for text, expected in [
            ("the space topic is too broad", "space"),
            ("finance is giving me junk", "finance"),
            ("fix my finance topic", "finance"),
            ("retune finance", "finance"),
        ]:
            intent = fallback_intent(text)
            self.assertEqual(intent.action, "retune_topic", text)
            self.assertEqual(intent.topic, expected, text)


class MigrationTests(unittest.TestCase):
    def test_a_database_from_before_the_interview_still_opens(self):
        # The Pi's database already has pending_topics without the new
        # columns; CREATE TABLE IF NOT EXISTS would not add them.
        path = Path(tempfile.mkdtemp()) / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """CREATE TABLE pending_topics (
                   user_id INTEGER PRIMARY KEY, label TEXT NOT NULL,
                   question TEXT NOT NULL, asked_at TEXT NOT NULL);"""
        )
        old.execute("INSERT INTO pending_topics VALUES (1, 'space', 'which?', ?)",
                    (datetime.now(timezone.utc).isoformat(),))
        old.commit()
        old.close()

        db = Database(path)
        db.connect()
        cols = {r["name"] for r in
                db.conn.execute("PRAGMA table_info(pending_topics)")}
        self.assertIn("transcript", cols)
        self.assertIn("asked", cols)
        db.close()


class LearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(
            telegram=TelegramConfig(token="t", whitelist=[7]),
            news=NewsConfig(gnews=GNewsConfig(api_key="")),
            digest=DigestConfig(default_time="08:00"),
        )
        self.ai = ScriptedAI({})
        self.bot = BotHandlers(self.cfg, self.db, self.ai,
                               FakeFetcher(ARTICLES), digest=None,
                               scheduler=None)
        await self.db.ensure_user(7, "john", "John")
        await self.db.set_timezone(7, "Europe/Amsterdam")
        await self.db.add_topic(7, "Satellites and Space", '"satellite launch"')
        await self.db.start_chat(7)

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def _say(self, text):
        update = FakeUpdate(7, text)
        await self.bot.on_text(update, FakeContext())
        return update.effective_message.replies

    async def _query(self):
        return (await self.db.list_topics(7))[0].query

    async def test_a_steer_changes_the_query_and_says_so(self):
        self.ai.replies = [
            {"search": None, "reply": "Noted.", "end": False,
             "steer": {"label": "Satellites and Space",
                       "change": "add reusable rockets"},
             "interests": []},
            REFINED,
        ]
        replies = await self._say("I want more on reusable rockets")
        self.assertIn("Tightened", replies[0])
        self.assertIn("added reusable rockets", replies[0])
        self.assertIn("reusable rockets", await self._query())

    async def test_one_busy_week_does_not_rewrite_anything(self):
        turn = {"search": "nvidia", "reply": None, "end": False, "steer": None,
                "interests": [{"label": "Satellites and Space",
                               "phrase": "starlink"}]}
        for _ in range(3):
            self.ai.replies = [turn, json.dumps(["a.", "b."]), "Answer [1]."]
            replies = await self._say("what about starlink")
            self.assertNotIn("Tightened", replies[0])
        # Recorded, watched, but not acted on.
        self.assertEqual(await self._query(), '"satellite launch"')
        self.assertEqual(await self.db.ripe_signals(7), [])

    async def test_an_interest_that_lasts_a_week_is_folded_in(self):
        turn = {"search": "starlink", "reply": None, "end": False,
                "steer": None,
                "interests": [{"label": "Satellites and Space",
                               "phrase": "starlink"}]}
        for _ in range(3):
            self.ai.replies = [turn, json.dumps(["a.", "b."]), "Answer [1]."]
            await self._say("what about starlink")

        # Age the first sighting past a week: still coming up, so it counts.
        self.db.conn.execute(
            "UPDATE topic_signals SET seen_at = ? WHERE id = 1",
            ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),),
        )
        self.db.conn.commit()

        self.ai.replies = [turn, json.dumps(["a.", "b."]), "Answer [1].",
                           {"query": '"satellite launch" OR Starlink',
                            "note": "added Starlink"}]
        replies = await self._say("anything new on starlink")
        self.assertIn("Tightened", replies[0])
        self.assertIn("Starlink", await self._query())
        # Applied once, not on every message from then on.
        self.assertEqual(await self.db.ripe_signals(7), [])

    async def test_an_interest_in_a_topic_they_do_not_follow_is_ignored(self):
        self.ai.replies = [
            {"search": "cricket", "reply": None, "end": False, "steer": None,
             "interests": [{"label": "Cricket", "phrase": "ashes"}]},
            json.dumps(["a.", "b."]), "Answer [1].",
        ]
        await self._say("what about the ashes")
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM topic_signals")
            .fetchone()[0], 0)


class TopicsListTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_list_shows_names_not_queries(self):
        tmp = tempfile.TemporaryDirectory()
        db = Database(Path(tmp.name) / "t.db")
        db.connect()
        cfg = Config(telegram=TelegramConfig(token="t", whitelist=[7]),
                     news=NewsConfig(gnews=GNewsConfig(api_key="")),
                     digest=DigestConfig(default_time="08:00"))
        bot = BotHandlers(cfg, db, ScriptedAI({}), None, None, None)
        await db.ensure_user(7, "john", "John")
        await db.set_timezone(7, "Europe/Amsterdam")
        await db.add_topic(7, "Satellites and Space",
                           '"satellite launch" OR "space exploration"')

        update = FakeUpdate(7)
        await bot.cmd_topics(update, FakeContext())
        body = update.effective_message.replies[0]
        self.assertIn("Satellites and Space", body)
        self.assertNotIn("satellite launch", body)
        db.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
