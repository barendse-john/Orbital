"""The app's Reports tab: who may read reports, the list, one report, settings."""

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
from newsbot.space import SpaceService
from newsbot.webapp import Ctx, start_web


class FakeRelay:
    def __init__(self, readers):
        self.readers = readers

    async def recipients(self):
        return list(self.readers)


class FakePusher:
    enabled = True
    public_key = ""

    def __init__(self):
        self.sent = []

    async def send(self, subs, payload):
        self.sent.append(payload)
        return []


class ReportsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        for uid in (7, 8):
            self.aw(self.db.ensure_user(uid, f"u{uid}", f"U{uid}"))
        self.pusher = FakePusher()
        self.app = AppService(self.db, None, None, self.pusher, "https://jb.ts.net")
        self.app.relay = FakeRelay([7])
        self.aw(self.db.save_report("f1", "2026-10-07_brief.md", "2026-10-07T03:55:00+00:00",
                                    "# Monday brief\n\nCash is **fine**."))
        self.aw(self.db.save_report("f2", "2026-10-08_brief.md", "2026-10-08T03:55:00+00:00",
                                    "# Tuesday brief\n\n- see [doc](https://x.io/a?b=1&c=2)\n- <script>"))
        space = SpaceService(SpaceConfig(), Path(self.tmp.name) / "c.json")
        self.server = start_web(space, "127.0.0.1", 0,
                                ctx=Ctx(space, None, self.app, self.loop, "https://jb.ts.net", "bot"))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.owner = self.aw(self.app.pair(7))
        self.friend = self.aw(self.app.pair(8))

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

    def test_only_recipients_see_reports(self):
        self.assertTrue(self.call("GET", "/api/me", token=self.owner)[1]["reports"])
        self.assertFalse(self.call("GET", "/api/me", token=self.friend)[1]["reports"])
        self.assertEqual(self.call("GET", "/api/me/reports", token=self.friend)[0], 403)
        self.assertEqual(self.call("GET", "/api/me/reports/f1", token=self.friend)[0], 403)
        self.assertEqual(self.call("GET", "/api/me/reports")[0], 401)

    def test_no_relay_means_no_reports(self):
        self.app.relay = None
        self.assertFalse(self.call("GET", "/api/me", token=self.owner)[1]["reports"])
        self.assertEqual(self.call("GET", "/api/me/reports", token=self.owner)[0], 403)

    def test_list_is_newest_first_with_titles(self):
        code, r = self.call("GET", "/api/me/reports", token=self.owner)
        self.assertEqual(code, 200)
        self.assertEqual([x["title"] for x in r["reports"]], ["Tuesday brief", "Monday brief"])
        self.assertEqual(r["reports"][1]["preview"], "Cash is fine.")

    def test_one_report_is_escaped_html(self):
        code, r = self.call("GET", "/api/me/reports/f2", token=self.owner)
        self.assertEqual(code, 200)
        html = "\n".join(r["blocks"])
        self.assertIn('<a href="https://x.io/a?b=1&amp;c=2">doc</a>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(self.call("GET", "/api/me/reports/nope", token=self.owner)[0], 404)

    def test_report_settings(self):
        code, me = self.call("POST", "/api/me/settings", {"report_push": False}, self.owner)
        self.assertEqual(code, 200)
        self.assertFalse(me["prefs"]["report_push"])
        self.assertEqual(self.call("POST", "/api/me/settings",
                                   {"report_telegram": False}, self.owner)[0], 400)

    def test_push_respects_the_setting(self):
        self.aw(self.db.add_push_sub(7, "https://push/x", "p", "a"))
        self.assertTrue(self.aw(self.app.report_push(7, "f2", "# Tuesday brief\n\nBody")))
        self.assertEqual(self.pusher.sent[0]["url"], "/app/#report=f2")
        self.assertEqual(self.pusher.sent[0]["title"], "📋 Tuesday brief")
        self.aw(self.db.set_app_pref(7, "report_push", False))
        self.assertFalse(self.aw(self.app.report_push(7, "f2", "# x")))


if __name__ == "__main__":
    unittest.main()
