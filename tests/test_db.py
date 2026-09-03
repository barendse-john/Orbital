"""Storage behaviour: per-user topics, dedupe, quota accounting."""

import tempfile
import unittest
from pathlib import Path

from newsbot.db import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_user_creation_is_idempotent(self):
        first = await self.db.ensure_user(1, "john", "John")
        again = await self.db.ensure_user(1, "john2", "John")
        self.assertEqual(first.user_id, again.user_id)
        self.assertEqual(again.username, "john2")
        self.assertEqual(len(await self.db.all_users()), 1)

    async def test_user_is_not_ready_until_set_up(self):
        user = await self.db.ensure_user(1, default_digest_time="08:00")
        self.assertFalse(user.is_ready)
        await self.db.set_timezone(1, "Europe/Amsterdam")
        self.assertTrue((await self.db.get_user(1)).is_ready)

    async def test_topics_are_per_user_and_unique(self):
        await self.db.ensure_user(1)
        await self.db.ensure_user(2)
        self.assertTrue(await self.db.add_topic(1, "Formula 1", "Formula 1"))
        self.assertFalse(await self.db.add_topic(1, "formula 1", "Formula 1"))
        self.assertTrue(await self.db.add_topic(2, "Formula 1", "Formula 1"))
        self.assertEqual(len(await self.db.list_topics(1)), 1)
        self.assertEqual(len(await self.db.list_topics(2)), 1)

    async def test_removal_matches_loosely(self):
        await self.db.ensure_user(1)
        await self.db.add_topic(1, "Manchester United", "Manchester United")
        self.assertEqual(await self.db.remove_topic(1, "manchester"),
                         "Manchester United")
        self.assertIsNone(await self.db.remove_topic(1, "manchester"))

    async def test_sent_articles_are_per_user(self):
        await self.db.mark_sent(1, [("k1", "https://a"), ("k2", "https://b")])
        self.assertEqual(await self.db.filter_unseen(1, ["k1", "k2", "k3"]), {"k3"})
        self.assertEqual(await self.db.filter_unseen(2, ["k1"]), {"k1"})

    async def test_marking_the_same_article_twice_is_safe(self):
        await self.db.mark_sent(1, [("k1", "https://a")])
        await self.db.mark_sent(1, [("k1", "https://a")])
        self.assertEqual((await self.db.stats(1))["articles_sent"], 1)

    async def test_quota_claims_stop_at_the_limit(self):
        self.assertTrue(await self.db.claim_gnews_call(2))
        self.assertTrue(await self.db.claim_gnews_call(2))
        self.assertFalse(await self.db.claim_gnews_call(2))
        await self.db.release_gnews_call()
        self.assertTrue(await self.db.claim_gnews_call(2))

    async def test_exhausting_the_quota_blocks_further_claims(self):
        await self.db.exhaust_gnews_today(100)
        self.assertEqual(await self.db.gnews_used_today(), 100)
        self.assertFalse(await self.db.claim_gnews_call(100))

    async def test_bootstrap_owner_claims_when_nobody_has(self):
        self.assertTrue(await self.db.bootstrap_owner(1))
        self.assertIn(1, await self.db.allowed_user_ids())
        self.assertIn(1, await self.db.admin_user_ids())

    async def test_bootstrap_only_claims_once(self):
        await self.db.bootstrap_owner(1)
        self.assertFalse(await self.db.bootstrap_owner(2))
        self.assertNotIn(2, await self.db.allowed_user_ids())
        self.assertNotIn(2, await self.db.admin_user_ids())

    async def test_bootstrap_refuses_once_someone_is_already_allowed(self):
        await self.db.allow_user(9)
        self.assertFalse(await self.db.bootstrap_owner(1))
        self.assertNotIn(1, await self.db.admin_user_ids())

    async def test_access_list_round_trip(self):
        await self.db.allow_user(99, added_by=1)
        self.assertIn(99, await self.db.allowed_user_ids())
        self.assertTrue(await self.db.deny_user(99))
        self.assertNotIn(99, await self.db.allowed_user_ids())

    async def test_settings_survive_a_reconnect(self):
        await self.db.ensure_user(1)
        await self.db.set_timezone(1, "Asia/Tokyo")
        await self.db.set_digest_time(1, "07:30")
        await self.db.add_topic(1, "Space", "space launches")
        self.db.close()

        reopened = Database(self.db.path)
        reopened.connect()
        user = await reopened.get_user(1)
        self.assertEqual((user.timezone, user.digest_time), ("Asia/Tokyo", "07:30"))
        self.assertEqual(len(await reopened.list_topics(1)), 1)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
