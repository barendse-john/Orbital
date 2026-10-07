"""Reports relayed from Google Drive: formatting, the Drive client and delivery.

No network and no google-auth needed: Drive runs on httpx.MockTransport and
the JWT signer is a stand-in.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx
from telegram.error import BadRequest, Forbidden, TimedOut

from newsbot.config import Config, ReportsConfig
from newsbot.db import Database
from newsbot.formatting import SAFE_LIMIT
from newsbot.reports import (DriveClient, DriveError, Report, ReportRelay,
                             build_relay, html_to_plain, md_to_html_blocks,
                             parse_time, report_messages)

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

    def test_long_report_splits_without_breaking_tags(self):
        md = "\n\n".join(f"## Section {i}\n- **item** [link](https://e.com/{i}) "
                         + "words " * 60 for i in range(40))
        messages = report_messages(md)
        self.assertGreater(len(messages), 1)
        for m in messages:
            self.assertLessEqual(len(m), SAFE_LIMIT)
            self.assertEqual(m.count("<b>"), m.count("</b>"))
            self.assertEqual(m.count("<a "), m.count("</a>"))

    def test_blank_report_is_no_messages(self):
        self.assertEqual(report_messages("\n  \n"), [])

    def test_plain_fallback_keeps_link_targets(self):
        self.assertEqual(html_to_plain('<b>A&amp;B</b> <a href="https://e.com">doc</a>'),
                         "A&B doc (https://e.com)")

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


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, parse_mode)
        self.fail = []          # exceptions to raise, in order

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((chat_id, text, parse_mode))

    async def send_document(self, chat_id, fh, filename=None):
        self.sent.append((chat_id, f"file:{filename}", None))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.db.connect()
        await self.db.bootstrap_owner(42)
        self.drive = FakeDrive(
            [Report("new", "2026-10-08_brief_v01.md", NOW - timedelta(hours=2))],
            {"new": "# Morning\n\n- **one**"})
        self.bot = FakeBot()
        self.relay = self.make_relay()

    def make_relay(self, **cfg):
        relay = ReportRelay(ReportsConfig(folder_id="F", **cfg), self.db, self.drive,
                            Path(self.tmp.name) / "reports")
        relay.pause = 0
        return relay

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_new_report_goes_to_the_owner_once(self):
        self.assertEqual(await self.relay.poll(self.bot, NOW), 1)
        self.assertEqual(self.bot.sent, [(42, "<b>Morning</b>\n\n• <b>one</b>", "HTML")])
        self.assertTrue((Path(self.tmp.name) / "reports" / "2026-10-08_brief_v01.md").exists())
        self.assertEqual(await self.relay.poll(self.bot, NOW), 0)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.drive.downloads, ["new"])     # no second download either

    async def test_friends_who_are_admins_do_not_get_reports(self):
        await self.db.add_admin(7, added_by=42)
        await self.relay.poll(self.bot, NOW)
        self.assertEqual({c for c, _, _ in self.bot.sent}, {42})

    async def test_chat_ids_override_the_owner(self):
        relay = self.make_relay(chat_ids=[5, 6])
        await relay.poll(self.bot, NOW)
        self.assertEqual([c for c, _, _ in self.bot.sent], [5, 6])

    async def test_old_reports_are_recorded_not_sent(self):
        self.drive.reports.insert(0, Report("old", "2026-10-01_brief.md",
                                            NOW - timedelta(days=7)))
        await self.relay.poll(self.bot, NOW)
        self.assertEqual(self.drive.downloads, ["new"])
        self.assertIn(("old", 42), await self.db.report_deliveries())

    async def test_failed_send_is_retried_next_poll(self):
        self.bot.fail = [TimedOut()]
        self.assertEqual(await self.relay.poll(self.bot, NOW), 0)
        self.assertEqual(await self.relay.poll(self.bot, NOW), 1)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_blocked_recipient_is_not_retried(self):
        self.bot.fail = [Forbidden("bot was blocked by the user")]
        await self.relay.poll(self.bot, NOW)
        await self.relay.poll(self.bot, NOW)
        self.assertEqual(self.bot.sent, [])

    async def test_rejected_html_falls_back_to_plain_text(self):
        self.bot.fail = [BadRequest("Can't parse entities")]
        await self.relay.poll(self.bot, NOW)
        self.assertEqual(self.bot.sent, [(42, "Morning\n\n• one", None)])

    async def test_attach_file(self):
        relay = self.make_relay(attach_file=True)
        await relay.poll(self.bot, NOW)
        self.assertEqual(self.bot.sent[-1], (42, "file:2026-10-08_brief_v01.md", None))

    async def test_nobody_to_send_to(self):
        db = Database(Path(self.tmp.name) / "empty.db")
        db.connect()
        relay = ReportRelay(ReportsConfig(folder_id="F"), db, self.drive,
                            Path(self.tmp.name) / "r")
        self.assertEqual(await relay.poll(self.bot, NOW), 0)
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

    def test_section_overrides(self):
        cfg = self.load("reports:\n  folder_id: xyz\n  chat_ids: [1, 2]\n"
                        "  poll_minutes: 1\n  attach_file: true\n")
        self.assertEqual(cfg.reports.folder_id, "xyz")
        self.assertEqual(cfg.reports.chat_ids, [1, 2])
        self.assertEqual(cfg.reports.poll_minutes, 5)       # floor
        self.assertTrue(cfg.reports.attach_file)


if __name__ == "__main__":
    unittest.main()
