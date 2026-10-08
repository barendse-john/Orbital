import asyncio
import json
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.config import SpaceConfig
from newsbot.db import Database
from newsbot.space import (SpaceService, launches_ics, alert_key, due_reminders,
                           parse_launch, parse_tle, send_launch_reminders)
from newsbot.news.models import Article
from newsbot.webapp import NewsProxy, start_web

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def raw_launch(**over):
    raw = {
        "id": "abc-1", "name": "Falcon 9 | Starlink 10-5",
        "net": "2026-09-20T12:25:00Z",
        "status": {"abbrev": "Go", "name": "Go for Launch"},
        "launch_service_provider": {"name": "SpaceX"},
        "rocket": {"configuration": {"full_name": "Falcon 9 Block 5",
                   "length": 70.0, "leo_capacity": "22800", "reusable": True,
                   "manufacturer": {"name": "SpaceX"}, "description": ""}},
        "mission": {"description": "Starlinks <b>", "orbit": {"name": "LEO"}},
        "pad": {"name": "SLC-40", "latitude": "28.56", "longitude": -80.57,
                "location": {"name": "Cape Canaveral, FL"}},
        "vid_urls": [{"url": "https://youtube.com/watch?v=x"}],
        "image": {"image_url": "https://img/x.jpg"},
    }
    raw.update(over)
    return raw


TLE = """ISS (ZARYA)
1 25544U 98067A   26262.50000000  .00016717  00000-0  10270-3 0  9005
2 25544  51.6400 208.9163 0006317  69.9862  25.2906 15.50377579 12345
garbage line
TIANGONG
1 48274U 21035A   26262.50000000  .00020000  00000-0  20000-3 0  9990
2 48274  41.4700 100.0000 0005000  50.0000  30.0000 15.60000000 12345
"""


class ParseTests(unittest.TestCase):
    def test_launch_fields(self):
        lnch = parse_launch(raw_launch())
        self.assertEqual(lnch.lat, 28.56)          # string coordinates coerced
        self.assertEqual(lnch.status, "Go")
        self.assertEqual(lnch.link, "https://youtube.com/watch?v=x")
        self.assertEqual(lnch.image, "https://img/x.jpg")
        self.assertTrue(lnch.net.endswith("Z"))

    def test_rocket_facts_keep_only_what_exists(self):
        info = parse_launch(raw_launch()).rocket_info
        self.assertEqual(info["leo_capacity_kg"], 22800.0)
        self.assertEqual(info["manufacturer"], "SpaceX")
        self.assertIs(info["reusable"], True)
        self.assertNotIn("description", info)
        self.assertEqual(parse_launch(raw_launch(rocket={})).rocket_info, {})

    def test_launch_without_pad_or_time_is_dropped(self):
        self.assertIsNone(parse_launch(raw_launch(pad={})))
        self.assertIsNone(parse_launch(raw_launch(net=None)))
        self.assertIsNone(parse_launch("nope"))

    def test_no_stream_falls_back_to_launch_page_never_google(self):
        lnch = parse_launch(raw_launch(vid_urls=[], slug="f9-sl-10-5"))
        self.assertEqual(lnch.link, "https://spacelaunchnow.me/launch/f9-sl-10-5/")
        lnch = parse_launch(raw_launch(vid_urls=[], info_urls=[
            {"url": "https://nextspaceflight.com/launches/1"},
            {"url": "https://www.spacex.com/launches/sl-10-5"}]))
        self.assertEqual(lnch.link, "https://www.spacex.com/launches/sl-10-5")

    def test_youtube_and_the_providers_own_stream_win(self):
        lnch = parse_launch(raw_launch(vid_urls=[
            {"url": "https://x.com/SpaceX/status/1", "publisher": "SpaceX"},
            {"url": "https://www.youtube.com/watch?v=fan", "publisher": "Fan Channel"},
            {"url": "https://www.youtube.com/watch?v=own", "publisher": "SpaceX"}]))
        self.assertEqual(lnch.link, "https://www.youtube.com/watch?v=own")

    def test_calendar_feed(self):
        lnch = parse_launch(raw_launch(name="Falcon 9 | Starlink, Group; 10-5"))
        ics = launches_ics([lnch], "http://jb:8080", NOW).decode()
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("UID:abc-1@newsbot-launches", ics)
        self.assertIn("DTSTART:20260920T122500Z", ics)
        self.assertIn("Starlink\\, Group\\; 10-5", ics)
        self.assertIn("TRIGGER:-PT30M", ics)
        self.assertTrue(all(len(l.encode()) <= 75 for l in ics.split("\r\n")))

    def test_tle_resyncs_past_garbage(self):
        sats = parse_tle(TLE, limit=10)
        self.assertEqual([s[0] for s in sats], ["ISS (ZARYA)", "TIANGONG"])
        self.assertEqual(len(parse_tle(TLE, limit=1)), 1)



class ReminderTests(unittest.TestCase):
    def test_tightest_lead_only(self):
        lnch = parse_launch(raw_launch())           # 25 min away
        self.assertEqual(due_reminders([lnch], NOW, [1440, 30])[0][1], 30)
        later = parse_launch(raw_launch(net="2026-09-21T02:00:00Z"))
        self.assertEqual(due_reminders([later], NOW, [1440, 30])[0][1], 1440)
        far = parse_launch(raw_launch(net="2026-09-25T02:00:00Z"))
        self.assertEqual(due_reminders([far], NOW, [1440, 30]), [])

    def test_tbd_and_past_launches_are_skipped(self):
        tbd = parse_launch(raw_launch(status={"abbrev": "TBD"}))
        past = parse_launch(raw_launch(net="2026-09-20T11:00:00Z"))
        self.assertEqual(due_reminders([tbd, past], NOW, [30]), [])

    def test_slipped_launch_gets_a_new_key(self):
        a = parse_launch(raw_launch())
        b = parse_launch(raw_launch(net="2026-09-20T13:00:00Z"))
        self.assertNotEqual(alert_key(a, 30), alert_key(b, 30))

    def test_claim_is_once_per_user(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / "t.db")
                db.connect()
                await db.set_launch_alerts(7, True)
                self.assertEqual(await db.launch_alert_users(), [(7, None)])
                self.assertTrue(await db.claim_launch_alert(7, "k"))
                self.assertFalse(await db.claim_launch_alert(7, "k"))
                await db.set_launch_alerts(7, False)
                self.assertEqual(await db.launch_alert_users(), [])
                db.close()
        asyncio.run(run())


class ReminderPushTests(unittest.IsolatedAsyncioTestCase):
    """Reminders go to the owner's phone, once, and to nobody else."""

    async def test_owner_gets_one_push_per_reminder(self):
        class App:
            def __init__(self):
                self.pushed = []

            async def launch_push(self, user_id, launch, lead, when):
                self.pushed.append((user_id, launch.id, lead))
                return True

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            db.connect()
            owner = await db.ensure_owner()
            await db.ensure_user(99, "friend", "Friend")   # from the Telegram days
            await db.set_launch_alerts(owner.user_id, True)
            await db.set_launch_alerts(99, True)
            space = SpaceService(SpaceConfig(), Path(tmp) / "c.json")
            soon = (datetime.now(timezone.utc) + timedelta(minutes=20))
            space.launches = [parse_launch(raw_launch(
                net=soon.strftime("%Y-%m-%dT%H:%M:%SZ")))]
            app = App()
            self.assertEqual(await send_launch_reminders(db, space, [1440, 30], app), 1)
            self.assertEqual(await send_launch_reminders(db, space, [1440, 30], app), 0)
            self.assertEqual(app.pushed, [(owner.user_id, "abc-1", 30)])
            await space.close()
            db.close()


class FakeRSS:
    def __init__(self):
        self.calls = []

    async def search(self, query, *, limit, lookback_hours):
        self.calls.append(query)
        return [Article(title=f"About {query}", url="https://ex.com/a",
                        source="Ex")]


class WebTests(unittest.TestCase):
    def test_news_proxy_runs_on_the_loop_and_caches(self):
        import threading
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            rss = FakeRSS()
            proxy = NewsProxy(loop, rss)
            self.assertEqual(proxy.get("  Kenya ")[0]["title"], "About Kenya")
            proxy.get("kenya")
            self.assertEqual(rss.calls, ["Kenya"])      # second hit cached
            self.assertEqual(proxy.get("   "), [])
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def test_serves_snapshot_and_page_but_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            space = SpaceService(SpaceConfig(), Path(tmp) / "cache.json")
            space.launches = [parse_launch(raw_launch())]
            space.satellites = {"stations": parse_tle(TLE, 10)}
            space.sats_at = {"stations": 1.0}
            space._rebuild()
            server = start_web(space, "127.0.0.1", 0)
            port = server.server_address[1]
            try:
                get = lambda p: urllib.request.urlopen(f"http://127.0.0.1:{port}{p}")
                data = json.load(get("/api/launches"))
                self.assertEqual(data["launches"][0]["id"], "abc-1")
                sats = json.load(get("/api/satellites"))
                self.assertEqual(len(sats["groups"]["stations"]), 2)
                self.assertIn(b"Cesium", get("/").read())
                with self.assertRaises(urllib.error.HTTPError):
                    get("/../config.yaml")
            finally:
                server.shutdown()
                server.server_close()

            # A restart serves the cached copy straight away.
            again = SpaceService(SpaceConfig(), Path(tmp) / "cache.json")
            self.assertEqual(again.launches[0].id, "abc-1")
            self.assertIn(b"abc-1", again.launches_json)


if __name__ == "__main__":
    unittest.main()
