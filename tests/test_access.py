"""A stranger asks, the owner taps a button, and nobody else can."""

import tempfile
import unittest
from pathlib import Path

from newsbot.config import Config, DigestConfig, GNewsConfig, NewsConfig, TelegramConfig
from newsbot.db import Database
from newsbot.handlers import BotHandlers

from tests.test_topics import FakeContext, FakeUpdate, ScriptedAI

OWNER = 7
STRANGER = 99
OUTSIDER = 55


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def send_chat_action(self, *a, **kw):
        pass


class FakeQueryMessage:
    def __init__(self, text):
        self.text_html = text
        self.edits = []
        self.markup_cleared = False


class FakeQuery:
    def __init__(self, data, from_id, text="🙋 Someone wants access."):
        self.data = data
        self.from_user = type("U", (), {"id": from_id, "first_name": "John"})()
        self.message = FakeQueryMessage(text)
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.message.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.markup_cleared = True


class CallbackUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user
        self.effective_message = None


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.cfg = Config(
            telegram=TelegramConfig(token="t", whitelist=[OWNER],
                                    admins=[OWNER]),
            news=NewsConfig(gnews=GNewsConfig(api_key="")),
            digest=DigestConfig(default_time="08:00"),
        )
        self.bot = BotHandlers(self.cfg, self.db, ScriptedAI({}), fetcher=None,
                               digest=None, scheduler=None)
        self.context = FakeContext()
        self.context.bot = FakeBot()

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def _stranger_says(self, text):
        update = FakeUpdate(STRANGER, text)
        await self.bot.on_text(update, self.context)
        return update.effective_message.replies

    async def _press(self, action, target=STRANGER, by=OWNER):
        query = FakeQuery(f"access:{action}:{target}", by)
        await self.bot.on_access_decision(CallbackUpdate(query), self.context)
        return query

    async def test_a_stranger_raises_a_request_and_the_owner_is_asked(self):
        replies = await self._stranger_says("hi can I use this?")
        self.assertIn("asked the owner", replies[0])

        row = await self.db.get_access_request(STRANGER)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["note"], "hi can I use this?")

        chat_id, body, kwargs = self.context.bot.sent[0]
        self.assertEqual(chat_id, OWNER)
        self.assertIn("hi can I use this?", body)
        self.assertIsNotNone(kwargs.get("reply_markup"))

    async def test_pestering_the_bot_asks_the_owner_once(self):
        await self._stranger_says("hello?")
        await self._stranger_says("hello??")
        await self._stranger_says("anyone there")
        self.assertEqual(len(self.context.bot.sent), 1)
        self.assertIn("Still waiting", (await self._stranger_says("hi"))[0])

    async def test_approving_lets_them_in(self):
        await self._stranger_says("please")
        query = await self._press("approve")

        self.assertIn(STRANGER, await self.db.allowed_user_ids())
        self.assertIn("✅ Approved", query.message.edits[0])
        told = [s for s in self.context.bot.sent if s[0] == STRANGER]
        self.assertIn("You're in", told[0][1])

    async def test_denying_keeps_them_out_and_does_not_ask_again(self):
        await self._stranger_says("let me in")
        await self._press("deny")

        self.assertNotIn(STRANGER, await self.db.allowed_user_ids())
        before = len(self.context.bot.sent)
        replies = await self._stranger_says("go on")
        self.assertEqual(replies[0], "This is a private bot.")
        # A denied person cannot make the owner's phone buzz again.
        self.assertEqual(len(self.context.bot.sent), before)

    async def test_only_admins_can_press_the_buttons(self):
        await self._stranger_says("hi")
        query = await self._press("approve", by=OUTSIDER)
        self.assertEqual(query.answers, ["Admins only."])
        self.assertNotIn(STRANGER, await self.db.allowed_user_ids())
        self.assertEqual((await self.db.get_access_request(STRANGER))["status"],
                         "pending")

    async def test_a_second_admin_pressing_a_stale_button_changes_nothing(self):
        await self._stranger_says("hi")
        await self.db.add_admin(OUTSIDER)
        await self._press("approve")
        query = await self._press("deny", by=OUTSIDER)

        self.assertIn("Already approved.", query.answers)
        self.assertTrue(query.message.markup_cleared)
        self.assertIn(STRANGER, await self.db.allowed_user_ids())

    async def test_allow_by_hand_settles_the_request_too(self):
        await self._stranger_says("hi")
        update = FakeUpdate(OWNER)
        await self.bot.cmd_allow(update, FakeContext(args=[str(STRANGER)]))
        self.assertEqual((await self.db.get_access_request(STRANGER))["status"],
                         "approved")

    async def test_requests_lists_who_asked(self):
        await self._stranger_says("hi there")
        update = FakeUpdate(OWNER)
        await self.bot.cmd_requests(update, self.context)
        body = update.effective_message.replies[0]
        self.assertIn("1 waiting", body)
        self.assertIn(str(STRANGER), body)

    async def test_users_lists_who_has_access_and_why(self):
        await self._stranger_says("hi")
        await self._press("approve")
        await self.db.ensure_user(STRANGER, "samwise", "Sam")

        update = FakeUpdate(OWNER)
        await self.bot.cmd_users(update, self.context)
        body = update.effective_message.replies[0]
        self.assertIn("Sam", body)
        self.assertIn("@samwise", body)
        self.assertIn("admin", body)          # the owner
        self.assertIn("in config.yaml", body)

    async def test_users_flags_an_id_that_never_messaged(self):
        # The usual cause is a typo in /allow, and it looks identical to a
        # working entry unless it is called out.
        await self.db.allow_user(123456)
        update = FakeUpdate(OWNER)
        await self.bot.cmd_users(update, self.context)
        self.assertIn("never messaged", update.effective_message.replies[0])

    async def test_users_is_admins_only(self):
        await self.db.allow_user(OUTSIDER)
        update = FakeUpdate(OUTSIDER)
        await self.bot.cmd_users(update, self.context)
        self.assertEqual(update.effective_message.replies, ["Admins only."])

    async def test_requests_is_admins_only(self):
        await self.db.allow_user(OUTSIDER)
        update = FakeUpdate(OUTSIDER)
        await self.bot.cmd_requests(update, self.context)
        self.assertEqual(update.effective_message.replies, ["Admins only."])


if __name__ == "__main__":
    unittest.main()
