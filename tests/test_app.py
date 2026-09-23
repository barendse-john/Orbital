import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from newsbot.appservice import AppService
from newsbot.config import SpaceConfig
from newsbot.db import Database
from newsbot.push import Pusher
from newsbot.space import SpaceService
from newsbot.webapp import Ctx, start_web


class FakeDigest:
    def __init__(self):
        self.last_items = {}
        self.app = None

    async def collect(self, user):
        self.last_items[user.user_id] = [{"topic": "Space", "title": "Starship flies",
                                          "url": "https://ex.com/s", "source": "Ex",
                                          "summary": "", "published": "", "image": ""}]


class AppApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        self.aw(self.db.ensure_user(7, "john", "John"))
        self.pusher = Pusher(Path(self.tmp.name))
        self.app = AppService(self.db, None, FakeDigest(), self.pusher, "https://jb.ts.net")
        space = SpaceService(SpaceConfig(), Path(self.tmp.name) / "c.json")
        self.server = start_web(space, "127.0.0.1", 0,
                                ctx=Ctx(space, None, self.app, self.loop, "https://jb.ts.net", "bot"))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.aw(self.app.pair(7))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.db.close()
        self.tmp.cleanup()

    def aw(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_needs_a_valid_token(self):
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", token="nope")[0], 401)
        code, me = self.call("GET", "/api/me", token=self.token)
        self.assertEqual((code, me["name"]), (200, "John"))

    def test_settings_validate_and_persist(self):
        code, _ = self.call("POST", "/api/me/settings", {"digest_time": "25:00"}, self.token)
        self.assertEqual(code, 400)
        code, me = self.call("POST", "/api/me/settings",
                             {"digest_time": "7:05", "digest_telegram": False,
                              "launch_alerts": True}, self.token)
        self.assertEqual(code, 200)
        self.assertEqual(me["digest_time"], "07:05")
        self.assertFalse(me["prefs"]["digest_telegram"])
        self.assertTrue(me["launch_alerts"])
        self.assertFalse(self.aw(self.app.wants_telegram_digest(7)))

    def test_topics_without_a_model_fall_back_to_a_plain_query(self):
        code, r = self.call("POST", "/api/me/topics", {"label": "Rocket Lab"}, self.token)
        self.assertEqual((code, r["added"]), (200, "Rocket Lab"))
        code, r = self.call("DELETE", "/api/me/topics", {"label": "Rocket Lab"}, self.token)
        self.assertEqual((code, r["topics"]), (200, []))

    def test_briefing_refresh_is_saved_and_rate_limited(self):
        self.assertEqual(self.call("POST", "/api/me/briefing/refresh", {}, self.token)[0], 400)
        self.call("POST", "/api/me/topics", {"label": "Space"}, self.token)
        code, r = self.call("POST", "/api/me/briefing/refresh", {}, self.token)
        self.assertEqual((code, r["new"]), (200, 1))
        self.assertEqual(self.call("POST", "/api/me/briefing/refresh", {}, self.token)[0], 429)
        code, r = self.call("GET", "/api/me/briefing", token=self.token)
        self.assertEqual(r["briefings"][0]["items"][0]["title"], "Starship flies")

    def test_push_subscription_checks_its_shape(self):
        bad = {"subscription": {"endpoint": "http://x", "keys": {}}}
        self.assertEqual(self.call("POST", "/api/me/push", bad, self.token)[0], 400)
        good = {"subscription": {"endpoint": "https://fcm.googleapis.com/x",
                                 "keys": {"p256dh": "a", "auth": "b"}}}
        self.assertEqual(self.call("POST", "/api/me/push", good, self.token)[0], 200)
        self.assertEqual(len(self.aw(self.db.push_subs(7))), 1)

    def test_config_exposes_a_vapid_key_that_survives_restart(self):
        code, cfg = self.call("GET", "/api/app/config")
        self.assertEqual(code, 200)
        if self.pusher.enabled:
            self.assertEqual(len(cfg["vapid"]), 87)       # 65-byte point, base64url
            self.assertEqual(Pusher(Path(self.tmp.name)).public_key, cfg["vapid"])

    def test_static_app_and_404(self):
        with urllib.request.urlopen(self.base + "/api/health") as r:
            self.assertEqual(r.status, 200)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/app/../config.yaml")


if __name__ == "__main__":
    unittest.main()
