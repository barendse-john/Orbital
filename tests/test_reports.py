"""Reports from Google Drive: formatting, the Drive client, storage and notifying.

No network and no google-auth needed: Drive runs on httpx.MockTransport and
the JWT signer is a stand-in.
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx

from newsbot.config import Config, ReportsConfig
from newsbot.db import Database
from newsbot.reports import (DriveClient, DriveError, Report, ReportRelay,
                             build_relay, md_to_html_blocks, parse_time,
                             report_preview, report_title)

NOW = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)


class MarkdownTests(unittest.TestCase):
    def test_subset_becomes_telegram_html(self):
        md = ("# Kalulu briefing\n\n- **Cash** is *fine*\n"
              "> quoted\nsee [the doc](https://x.io/a?b=1&c=2) and `a<b`\n\n---")
        blocks = md_to_html_blocks(md)
        self.assertEqual(blocks[0], "<b>Kalulu briefing</b>")
        self.assertIn("• <b>Cash</b> is <i>fine</i>", blocks[1])
        self.assertIn("<i>quoted</i>", blocks[1])
        self.assertIn('<a href="https://x.io/a?b=1&amp;c=2">the doc</a>', blocks[1])
        self.assertIn("<code>a&lt;b</code>", blocks[1])
        self.assertEqual(blocks[2], "──────────")

    def test_text_is_escaped(self):
        self.assertEqual(md_to_html_blocks("R&D <draft>"), ["R&amp;D &lt;draft&gt;"])

    def test_title_and_preview(self):
        md = "# **Kalulu** briefing\n\n- Cash is [fine](https://x.io)\n- Runway *9 months*\n"
        self.assertEqual(report_title(md, "x.md"), "Kalulu briefing")
        self.assertEqual(report_preview(md), "Cash is fine · Runway 9 months")
        self.assertEqual(report_title("no heading", "2026-10-08_brief.md"), "2026-10-08 brief")
        self.assertTrue(report_preview("word " * 100, 40).endswith("…"))

    def test_drive_times(self):
        self.assertEqual(parse_time("2026-10-08T03:51:12.345Z"),
                         datetime(2026, 10, 8, 3, 51, 12, 345000, tzinfo=timezone.utc))
        self.assertIsNotNone(parse_time("garbage").tzinfo)


def drive_with(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DriveClient("pi-relay@x.iam.gserviceaccount.com",
                       lambda claims: "signed." + claims["scope"], http=http)


class DriveClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_every_page_and_only_markdown(self):
        calls = {"token": 0, "list": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                calls["token"] += 1
                form = parse_qs(request.content.decode())
                self.assertTrue(form["assertion"][0].startswith("signed."))
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            self.assertEqual(request.headers["Authorization"], "Bearer tok")
            calls["list"] += 1
            self.assertIn("'FOLDER' in parents", request.url.params["q"])
            if "pageToken" not in request.url.params:
                return httpx.Response(200, json={"nextPageToken": "p2", "files": [
                    {"id": "a", "name": "2026-10-07_brief_v01.md",
                     "createdTime": "2026-10-07T03:55:00Z"},
                    {"id": "b", "name": "notes.md.txt", "createdTime": "2026-10-07T04:00:00Z"}]})
            return httpx.Response(200, json={"files": [
                {"id": "c", "name": "2026-10-08_brief_v01.md",
                 "createdTime": "2026-10-08T03:55:00Z"}]})

        drive = drive_with(handler)
        reports = await drive.list_reports("FOLDER")
        await drive.list_reports("FOLDER")
        await drive.close()
        self.assertEqual([r.id for r in reports], ["a", "c"])
        self.assertEqual(calls, {"token": 1, "list": 4})    # token reused

    async def test_download(self):
        def handler(request):
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "tok"})
            self.assertEqual(request.url.path, "/drive/v3/files/abc")
            self.assertEqual(request.url.params["alt"], "media")
            return httpx.Response(200, content="# Hé".encode())

        drive = drive_with(handler)
        self.assertEqual(await drive.download("abc"), "# Hé")
        await drive.close()

    async def test_errors_are_named(self):
        def handler(request):
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(404, json={"error": {"message": "File not found: FOLDER."}})

        drive = drive_with(handler)
        with self.assertRaisesRegex(DriveError, "404: File not found"):
            await drive.list_reports("FOLDER")
        await drive.close()

    async def test_refused_key_is_named(self):
        drive = drive_with(lambda r: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid JWT"}))
        with self.assertRaisesRegex(DriveError, "invalid_grant: Invalid JWT"):
            await drive.list_reports("FOLDER")
        await drive.close()

    async def test_timeout_is_a_drive_error(self):
        def handler(request):
            raise httpx.ConnectTimeout("")

        drive = drive_with(handler)
        with self.assertRaisesRegex(DriveError, "ConnectTimeout"):
            await drive.list_reports("FOLDER")
        await drive.close()


class FakeDrive:
    def __init__(self, reports, texts):
        self.reports = reports
        self.texts = texts
        self.downloads = []

    async def list_reports(self, folder_id):
        return list(self.reports)

    async def download(self, file_id):
        self.downloads.append(file_id)
        return self.texts[file_id]

    async def close(self):
        pass


class FakeApp:
    """Stands in for AppService.report_push: notifies unless switched off."""

    def __init__(self, db):
        self.db = db
        self.pushed = []
        self.fail = False

    async def report_push(self, user_id, file_id, text):
        if self.fail:
            raise RuntimeError("push service down")
        if not (await self.db.app_prefs(user_id))["report_push"]:
            return False
        self.pushed.append((user_id, file_id))
        return True


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        # The owner, as a database from the Telegram days records them.
        await asyncio.to_thread(
            self.db._write, "INSERT INTO admin_users (user_id, added_by, added_at) "
            "VALUES (42, NULL, '2026-09-01')", ())
        self.drive = FakeDrive(
            [Report("new", "2026-10-08_brief_v01.md", NOW - timedelta(hours=2))],
            {"new": "# Morning\n\n- **one**"})
        self.relay = self.make_relay()

    def make_relay(self, **cfg):
        relay = ReportRelay(ReportsConfig(folder_id="F", **cfg), self.db, self.drive,
                            Path(self.tmp.name) / "reports")
        relay.app = FakeApp(self.db)
        return relay

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_new_report_is_stored_and_the_owner_notified_once(self):
        self.assertEqual(await self.relay.poll(NOW), 1)
        self.assertEqual(self.relay.app.pushed, [(42, "new")])
        self.assertEqual((await self.db.get_report("new"))["body"], "# Morning\n\n- **one**")
        self.assertTrue((Path(self.tmp.name) / "reports" / "2026-10-08_brief_v01.md").exists())
        self.assertEqual(await self.relay.poll(NOW), 0)
        self.assertEqual(len(self.relay.app.pushed), 1)
        self.assertEqual(self.drive.downloads, ["new"])     # no second download either

    async def test_only_the_owner_gets_reports(self):
        # A friend's admin row from the Telegram days counts for nothing.
        await asyncio.to_thread(
            self.db._write, "INSERT INTO admin_users (user_id, added_by, added_at) "
            "VALUES (7, 42, '2026-09-02')", ())
        await self.relay.poll(NOW)
        self.assertEqual({u for u, _ in self.relay.app.pushed}, {42})

    async def test_old_reports_are_kept_for_the_app_without_notifying(self):
        self.drive.reports.insert(0, Report("old", "2026-10-01_brief.md",
                                            NOW - timedelta(days=7)))
        self.drive.reports.insert(0, Report("ancient", "2026-08-01_brief.md",
                                            NOW - timedelta(days=60)))
        self.drive.texts["old"] = "# Last week"
        await self.relay.poll(NOW)
        self.assertEqual(self.drive.downloads, ["old", "new"])   # ancient stays in Drive
        self.assertEqual(self.relay.app.pushed, [(42, "new")])
        self.assertIn(("old", 42), await self.db.report_deliveries())
        self.assertEqual([r["file_id"] for r in await self.db.list_reports()],
                         ["new", "old"])

    async def test_reports_sent_on_telegram_before_are_backfilled_silently(self):
        await self.db.claim_report("new", 42, "2026-10-08_brief_v01.md")
        await self.relay.poll(NOW)
        self.assertEqual(self.relay.app.pushed, [])
        self.assertIsNotNone(await self.db.get_report("new"))

    async def test_notifications_off_still_stores(self):
        await self.db.set_app_pref(42, "report_push", False)
        self.assertEqual(await self.relay.poll(NOW), 0)
        self.assertIsNotNone(await self.db.get_report("new"))

    async def test_a_failed_push_does_not_stop_the_poll(self):
        self.relay.app.fail = True
        self.assertEqual(await self.relay.poll(NOW), 0)
        self.assertIsNotNone(await self.db.get_report("new"))

    async def test_nobody_to_show_them_to(self):
        db = Database(Path(self.tmp.name) / "empty.db")
        db.connect()
        relay = ReportRelay(ReportsConfig(folder_id="F"), db, self.drive,
                            Path(self.tmp.name) / "r")
        self.assertEqual(await relay.poll(NOW), 0)
        self.assertEqual(self.drive.downloads, [])
        db.close()


class ConfigTests(unittest.TestCase):
    def load(self, extra: str = "", env: dict | None = None) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("telegram:\n  token: t\nai:\n  anthropic:\n    api_key: k\n"
                            f"database:\n  path: {tmp}/d.db\n" + extra)
            clean = {k: v for k, v in os.environ.items()
                     if k not in ("GDRIVE_REPORTS_FOLDER_ID", "GOOGLE_SERVICE_ACCOUNT_FILE")}
            with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
                return Config.load(path)

    def test_off_without_a_folder(self):
        cfg = self.load()
        relay, why = build_relay(cfg, db=None)
        self.assertIsNone(relay)
        self.assertIn("GDRIVE_REPORTS_FOLDER_ID", why)

    def test_env_alone_is_enough_to_configure(self):
        cfg = self.load(env={"GDRIVE_REPORTS_FOLDER_ID": "abc"})
        self.assertEqual(cfg.reports.folder_id, "abc")
        self.assertEqual(cfg.reports.poll_minutes, 15)
        relay, why = build_relay(cfg, db=None)
        self.assertIsNone(relay)
        self.assertIn("not found", why)                     # no key file here

    def test_needs_the_app(self):
        cfg = self.load("space:\n  enabled: false\n", env={"GDRIVE_REPORTS_FOLDER_ID": "abc"})
        relay, why = build_relay(cfg, db=None)
        self.assertIsNone(relay)
        self.assertIn("Orbital", why)

    def test_section_overrides(self):
        cfg = self.load("reports:\n  folder_id: xyz\n  poll_minutes: 1\n")
        self.assertEqual(cfg.reports.folder_id, "xyz")
        self.assertEqual(cfg.reports.poll_minutes, 5)       # floor


if __name__ == "__main__":
    unittest.main()
