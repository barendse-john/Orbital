"""Reports from a Google Drive folder, shown in the Orbital app.

A cloud routine writes a markdown report (the Kalulu morning briefing) into a
Drive folder every morning. This polls that folder, keeps each new report in
the database (the app's Reports tab reads it from there) and under
data/reports/, and sends a phone notification to whoever reads reports - the
bot's owner by default. Nothing goes to Telegram: John reads these in the app.

Drive is read with a service account, a Google identity with no browser
login, which suits a headless Pi. Its scope is drive.readonly and it sees only
the folder that was shared with it.

As with the Anthropic backend there is no Google SDK: google-auth signs the
token request and the two Drive calls go over plain httpx. google-auth is
optional. Without it, a key file or a folder id the feature switches itself
off with one log line and the rest of the bot carries on.

Ported from John's standalone briefing_relay.py (cron, its own JSON state,
Telegram messages). Notification state now lives in SQLite, per report and
per reader, so a restart or an overlapping poll can't notify twice.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from .ai.base import describe

if TYPE_CHECKING:
    from .config import Config, ReportsConfig
    from .db import Database

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# How far back the app's Reports tab is filled the first time, on top of
# max_age_hours. Older reports stay in Drive only.
BACKFILL_DAYS = 14


class DriveError(RuntimeError):
    """Drive or Google's token endpoint refused, or could not be reached."""


# ------------------------------------------------------- markdown -> HTML ----
# The app shows a report as HTML paragraphs in a small, fully escaped subset:
# <b> <i> <code> and http(s) links. The reports use a small markdown subset,
# so a line-by-line converter is enough, and every tag closes on its line.

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    # Pull links and code out first so escaping and bold rules don't touch them.
    stash: list[str] = []

    def keep(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{html.escape(m.group(1))}</a>"
        ),
        text,
    )
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def md_line_to_html(line: str) -> str:
    stripped = line.strip()
    if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
        return "──────────"
    m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if m:
        return f"<b>{_inline(m.group(2))}</b>"
    m = re.match(r"^[-*+]\s+(.*)$", stripped)
    if m:
        return f"• {_inline(m.group(1))}"
    m = re.match(r"^>\s?(.*)$", stripped)
    if m:
        return f"<i>{_inline(m.group(1))}</i>"
    return _inline(line.rstrip())


def md_to_html_blocks(md: str) -> list[str]:
    """Markdown to a list of HTML paragraphs (split on blank lines)."""
    blocks: list[str] = []
    current: list[str] = []
    for line in md.splitlines():
        if line.strip():
            current.append(md_line_to_html(line))
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _plain(md_line: str) -> str:
    """One markdown line as bare text, for titles and previews."""
    text = _LINK.sub(r"\1", md_line.strip())
    text = re.sub(r"^(#{1,6}|[-*+]|>)\s+", "", text)
    return re.sub(r"[*_`]+", "", text).strip()


def report_title(md: str, name: str = "") -> str:
    """The first heading, else the file name without .md."""
    for line in md.splitlines():
        if re.match(r"^\s*#{1,6}\s+\S", line):
            return _plain(line)[:120]
    return re.sub(r"\.md$", "", name, flags=re.I).replace("_", " ") or "Report"


def report_preview(md: str, limit: int = 160) -> str:
    """The first lines of body text, for a list entry or a notification."""
    words: list[str] = []
    for line in md.splitlines():
        if not line.strip() or re.match(r"^\s*(#{1,6}\s|-{3,}|\*{3,}|_{3,})", line):
            continue
        words.append(_plain(line))
        if sum(len(w) for w in words) > limit:
            break
    text = " · ".join(w for w in words if w)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def report_summary(row: dict) -> dict:
    """What the app's list shows for one stored report."""
    return {"id": row["file_id"], "name": row["name"], "created": row["created_at"],
            "title": report_title(row["body"], row["name"]),
            "preview": report_preview(row["body"])}


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", Path(name).name).strip(" .") or "report.md"



# ------------------------------------------------------------------ drive ----

@dataclass
class Report:
    id: str
    name: str
    created: datetime


def parse_time(value: str) -> datetime:
    """Drive's RFC 3339 times end in 'Z', which 3.10's fromisoformat rejects.
    Unreadable means 'now', so a report is never skipped for its timestamp."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200].strip() or "no details"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):                       # Drive's error shape
        return str(err.get("message") or err)[:200]
    desc = body.get("error_description") if isinstance(body, dict) else None
    return f"{err}: {desc}" if desc else str(err or body)[:200]


class DriveClient:
    """Lists and downloads files in one Drive folder as a service account.

    `sign` turns a JWT claim set into a signed assertion. from_key_file()
    builds it with google-auth; tests pass a stand-in, so they need neither
    google-auth nor a key.
    """

    def __init__(self, email: str, sign: Callable[[dict], str], *,
                 token_uri: str = DEFAULT_TOKEN_URI,
                 http: httpx.AsyncClient | None = None):
        self.email = email
        self._sign = sign
        self.token_uri = token_uri
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0))
        self._token = ""
        self._expires = 0.0

    @classmethod
    def from_key_file(cls, path: str | Path,
                      http: httpx.AsyncClient | None = None) -> "DriveClient":
        """ImportError without google-auth; OSError, KeyError or ValueError
        for a missing or malformed key file."""
        from google.auth import crypt, jwt

        info = json.loads(Path(path).read_text(encoding="utf-8"))
        signer = crypt.RSASigner.from_service_account_info(info)

        def sign(claims: dict) -> str:
            token = jwt.encode(signer, claims)
            return token.decode() if isinstance(token, bytes) else token

        return cls(info["client_email"], sign,
                   token_uri=info.get("token_uri") or DEFAULT_TOKEN_URI,
                   http=http)

    async def _access_token(self) -> str:
        # Tokens last an hour; a 15-minute poll reuses one four times.
        if self._token and time.time() < self._expires - 60:
            return self._token
        now = int(time.time())
        assertion = self._sign({"iss": self.email, "scope": SCOPE,
                                "aud": self.token_uri, "iat": now,
                                "exp": now + 3600})
        try:
            r = await self.http.post(self.token_uri, data={
                "grant_type": JWT_GRANT, "assertion": assertion})
        except httpx.HTTPError as exc:
            raise DriveError(f"token request failed: {describe(exc)}") from exc
        if r.status_code != 200:
            raise DriveError(
                f"Google refused the service account ({r.status_code}): "
                f"{_error_text(r)}")
        data = r.json()
        self._token = str(data["access_token"])
        self._expires = time.time() + float(data.get("expires_in", 3600))
        return self._token

    async def _get(self, url: str, params: dict) -> httpx.Response:
        token = await self._access_token()
        try:
            r = await self.http.get(url, params=params,
                                    headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise DriveError(describe(exc)) from exc
        if r.status_code == 401:
            self._token = ""            # revoked or expired early: fresh one next time
        if r.status_code != 200:
            raise DriveError(f"Drive answered {r.status_code}: {_error_text(r)}")
        return r

    async def list_reports(self, folder_id: str) -> list[Report]:
        """Every .md file in the folder, oldest first."""
        folder = folder_id.replace("\\", "").replace("'", "")
        params = {
            "q": f"'{folder}' in parents and trashed = false and name contains '.md'",
            "fields": "nextPageToken, files(id, name, createdTime)",
            "orderBy": "createdTime",
            "pageSize": "100",
            # Harmless for My Drive, required if the folder is on a shared drive.
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        reports: list[Report] = []
        while True:
            data = (await self._get(DRIVE_FILES, params)).json()
            for f in data.get("files", []):
                name = str(f.get("name") or "")
                if name.lower().endswith(".md") and f.get("id"):
                    reports.append(Report(str(f["id"]), name,
                                          parse_time(f.get("createdTime", ""))))
            page = data.get("nextPageToken")
            if not page:
                return reports
            params["pageToken"] = page

    async def download(self, file_id: str) -> str:
        r = await self._get(f"{DRIVE_FILES}/{file_id}",
                            {"alt": "media", "supportsAllDrives": "true"})
        return r.content.decode("utf-8", errors="replace")

    async def close(self) -> None:
        await self.http.aclose()


# ------------------------------------------------------------------ relay ----

class ReportRelay:
    def __init__(self, cfg: "ReportsConfig", db: "Database", drive: DriveClient,
                 archive_dir: Path, admins: list[int] | None = None):
        self.cfg = cfg
        self.db = db
        self.drive = drive
        self.archive_dir = Path(archive_dir)
        self.admins = list(admins or [])
        self.app = None             # AppService, attached in __main__

    async def recipients(self) -> list[int]:
        """Who may read reports in the app and gets notified of new ones:
        reports.chat_ids if set, otherwise the bot's owner.

        Not every admin: admins can be friends, and these reports are John's.
        """
        if self.cfg.chat_ids:
            return list(dict.fromkeys(self.cfg.chat_ids))
        return sorted(set(self.admins) | await self.db.owner_ids())

    async def poll(self, now: datetime | None = None) -> int:
        """Store anything new and notify about it. Returns how many
        notifications went out.

        Raises DriveError when Drive can't be read; the next poll is the retry.
        """
        recipients = await self.recipients()
        if not recipients:
            log.warning("Reports: nobody to show them to yet. Message the bot "
                        "to claim it, or set reports.chat_ids")
            return 0
        reports = await self.drive.list_reports(self.cfg.folder_id)
        if not reports:
            return 0
        now = now or datetime.now(timezone.utc)
        await self._store_new(reports, now)

        done = await self.db.report_deliveries()
        cutoff = now - timedelta(hours=self.cfg.max_age_hours)
        notified = 0
        for report in reports:
            pending = [c for c in recipients if (report.id, c) not in done]
            if not pending:
                continue
            stored = await self.db.get_report(report.id)
            # A fresh install, or a Pi that was off for days, would otherwise
            # ring the phone for a pile of stale briefings. They are still in
            # the app's list.
            fresh = stored is not None and report.created >= cutoff
            for chat_id in pending:
                if not await self.db.claim_report(
                        report.id, chat_id, report.name,
                        status="sent" if fresh else "skipped"):
                    continue        # an overlapping poll got there first
                if fresh and await self._notify(chat_id, report, stored["body"]):
                    notified += 1
        return notified

    async def _store_new(self, reports: list[Report], now: datetime) -> None:
        """Keep the text of every recent report, for the app and the archive.
        The first run also fills the app with the last two weeks."""
        known = await self.db.report_ids()
        horizon = now - timedelta(days=BACKFILL_DAYS,
                                  hours=self.cfg.max_age_hours)
        for report in reports:
            if report.id in known or report.created < horizon:
                continue
            text = await self.drive.download(report.id)
            self._archive(report.name, text)
            await self.db.save_report(report.id, report.name,
                                      report.created.isoformat(timespec="seconds"),
                                      text)
            log.info("Report %s saved for the app", report.name)

    async def _notify(self, chat_id: int, report: Report, text: str) -> bool:
        if self.app is None:
            return False
        try:
            if await self.app.report_push(chat_id, report.id, text):
                log.info("Report %s: notified %s", report.name, chat_id)
                return True
        except Exception as exc:  # noqa: BLE001 - the report is in the app either way
            log.warning("Report notification to %s failed: %s",
                        chat_id, describe(exc))
        return False

    def _archive(self, name: str, text: str) -> Path | None:
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            path = self.archive_dir / _safe_name(name)
            path.write_text(text, encoding="utf-8")
            return path
        except OSError as exc:
            log.warning("Could not archive report %s: %s", name, describe(exc))
            return None

    async def close(self) -> None:
        await self.drive.close()


def build_relay(cfg: "Config", db: "Database") -> tuple[ReportRelay | None, str]:
    """(relay, "") when reports are set up, else (None, why not)."""
    rc = cfg.reports
    if not rc.enabled:
        return None, "switched off in config.yaml"
    if not rc.folder_id:
        return None, "no Drive folder (set GDRIVE_REPORTS_FOLDER_ID in .env)"
    if not cfg.space.enabled:
        return None, "reports are read in the Orbital app, which needs space.enabled"
    key = Path(rc.service_account_file).expanduser()
    if not key.exists():
        return None, f"service account key {key} not found"
    try:
        drive = DriveClient.from_key_file(key)
    except ImportError:
        return None, "google-auth not installed (pip install -r requirements.txt)"
    except Exception as exc:  # noqa: BLE001 - a bad key must not stop the bot
        return None, f"unusable key {key}: {describe(exc)}"
    archive = (Path(rc.archive_dir).expanduser() if rc.archive_dir
               else cfg.database_path.parent / "reports")
    return ReportRelay(rc, db, drive, archive, admins=cfg.telegram.admins), ""
