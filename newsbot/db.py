"""SQLite storage. Everything survives a restart or a power cut on the Pi.

The sqlite3 calls are synchronous but tiny; each public method hops onto a
worker thread so the asyncio event loop is never blocked, and a single lock
keeps concurrent writes honest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Notification switches in the app's Settings tab. All on by default.
PREF_KEYS = ("digest_push", "launch_push", "breaking_push", "report_push")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    timezone      TEXT,
    digest_time   TEXT,
    digest_enabled INTEGER NOT NULL DEFAULT 1,
    onboarded     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    label      TEXT NOT NULL,
    query      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS topics_user_label
    ON topics (user_id, lower(label));

CREATE TABLE IF NOT EXISTS sent_articles (
    user_id     INTEGER NOT NULL,
    article_key TEXT NOT NULL,
    url         TEXT,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (user_id, article_key)
);
CREATE INDEX IF NOT EXISTS sent_articles_sent_at ON sent_articles (sent_at);

CREATE TABLE IF NOT EXISTS api_usage (
    day   TEXT PRIMARY KEY,
    gnews_calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS launch_alerts (
    user_id    INTEGER PRIMARY KEY,
    enabled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS launch_alerts_sent (
    user_id   INTEGER NOT NULL,
    alert_key TEXT NOT NULL,
    sent_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, alert_key)
);

CREATE TABLE IF NOT EXISTS app_tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT
);

CREATE TABLE IF NOT EXISTS push_subs (
    endpoint   TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_prefs (
    user_id         INTEGER PRIMARY KEY,
    digest_push     INTEGER NOT NULL DEFAULT 1,
    launch_push     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS briefings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    items      TEXT NOT NULL
);

-- Reports from Google Drive: one row per report and reader, so a restart or
-- an overlapping poll can't notify twice. 'skipped' means it was already old
-- when first seen and was listed in the app without a notification.
CREATE TABLE IF NOT EXISTS report_deliveries (
    file_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    name    TEXT,
    status  TEXT NOT NULL DEFAULT 'sent',
    at      TEXT NOT NULL,
    PRIMARY KEY (file_id, chat_id)
);

-- The text of each report from Drive, for the app's Reports tab.
CREATE TABLE IF NOT EXISTS reports (
    file_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT NOT NULL
);
"""


@dataclass
class User:
    user_id: int
    username: str | None
    first_name: str | None
    timezone: str | None
    digest_time: str | None
    digest_enabled: bool
    onboarded: bool

    @property
    def is_ready(self) -> bool:
        """True once we know where they are and when they want the digest."""
        return bool(self.timezone and self.digest_time)


@dataclass
class Topic:
    id: int
    label: str
    query: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- setup

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        log.info("Database ready at %s", self.path)

    def _migrate(self) -> None:
        """Columns added after a release. CREATE TABLE IF NOT EXISTS won't
        add them to a database that already exists on the Pi."""
        added = [
            ("app_prefs", "breaking_push", "INTEGER NOT NULL DEFAULT 1"),
            ("app_prefs", "report_push", "INTEGER NOT NULL DEFAULT 1"),
        ]
        for table, column, decl in added:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never called")
        return self._conn

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- users

    def _ensure_user_sync(
        self, user_id: int, username: str | None, first_name: str | None,
        default_digest_time: str,
    ) -> User:
        with self._lock:
            self.conn.execute(
                """INSERT INTO users (user_id, username, first_name, digest_time,
                                      created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username = excluded.username,
                       first_name = excluded.first_name,
                       last_seen_at = excluded.last_seen_at""",
                (user_id, username, first_name, default_digest_time,
                 _utcnow(), _utcnow()),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _row_to_user(row)

    async def ensure_user(
        self, user_id: int, username: str | None = None,
        first_name: str | None = None, default_digest_time: str = "08:00",
    ) -> User:
        return await asyncio.to_thread(
            self._ensure_user_sync, user_id, username, first_name,
            default_digest_time,
        )

    async def get_user(self, user_id: int) -> User | None:
        rows = await asyncio.to_thread(
            self._read, "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        return _row_to_user(rows[0]) if rows else None

    async def all_users(self) -> list[User]:
        rows = await asyncio.to_thread(self._read, "SELECT * FROM users")
        return [_row_to_user(r) for r in rows]

    async def set_timezone(self, user_id: int, tz: str) -> None:
        await asyncio.to_thread(
            self._write, "UPDATE users SET timezone = ? WHERE user_id = ?",
            (tz, user_id),
        )

    async def set_digest_time(self, user_id: int, hhmm: str) -> None:
        await asyncio.to_thread(
            self._write, "UPDATE users SET digest_time = ? WHERE user_id = ?",
            (hhmm, user_id),
        )

    async def set_digest_enabled(self, user_id: int, enabled: bool) -> None:
        await asyncio.to_thread(
            self._write,
            "UPDATE users SET digest_enabled = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )

    # --------------------------------------------------------------- topics

    async def add_topic(self, user_id: int, label: str, query: str) -> bool:
        """Returns False if the user already tracks this topic."""
        def _add() -> bool:
            try:
                self._write(
                    """INSERT INTO topics (user_id, label, query, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, label.strip(), query.strip(), _utcnow()),
                )
                return True
            except sqlite3.IntegrityError:
                return False
        return await asyncio.to_thread(_add)

    async def remove_topic(self, user_id: int, label: str) -> str | None:
        """Removes by exact (case-insensitive) label, else by substring."""
        def _remove() -> str | None:
            rows = self._read(
                "SELECT id, label FROM topics WHERE user_id = ?", (user_id,)
            )
            needle = label.strip().lower()
            match = next(
                (r for r in rows if r["label"].lower() == needle), None
            ) or next(
                (r for r in rows if needle in r["label"].lower()), None
            )
            if not match:
                return None
            self._write("DELETE FROM topics WHERE id = ?", (match["id"],))
            return match["label"]
        return await asyncio.to_thread(_remove)

    async def list_topics(self, user_id: int) -> list[Topic]:
        rows = await asyncio.to_thread(
            self._read,
            "SELECT id, label, query FROM topics WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        return [Topic(r["id"], r["label"], r["query"]) for r in rows]

    async def update_topic_query(self, user_id: int, label: str,
                                 query: str) -> bool:
        """Repoint an existing topic at a better search query."""
        def _update() -> bool:
            cur = self._write(
                """UPDATE topics SET query = ?
                   WHERE user_id = ? AND lower(label) = lower(?)""",
                (query.strip(), user_id, label.strip()),
            )
            return cur.rowcount > 0
        return await asyncio.to_thread(_update)

    # ------------------------------------------------------------ dedupe

    async def filter_unseen(self, user_id: int, keys: list[str]) -> set[str]:
        """Of the given article keys, which has this user not been sent?"""
        if not keys:
            return set()
        def _filter() -> set[str]:
            seen: set[str] = set()
            # Chunked to stay under SQLite's variable limit.
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                rows = self._read(
                    f"""SELECT article_key FROM sent_articles
                        WHERE user_id = ? AND article_key IN ({marks})""",
                    (user_id, *chunk),
                )
                seen.update(r["article_key"] for r in rows)
            return set(keys) - seen
        return await asyncio.to_thread(_filter)

    async def mark_sent(self, user_id: int, items: list[tuple[str, str]]) -> None:
        """items: (article_key, url) pairs."""
        if not items:
            return
        def _mark() -> None:
            now = _utcnow()
            with self._lock:
                self.conn.executemany(
                    """INSERT OR IGNORE INTO sent_articles
                       (user_id, article_key, url, sent_at) VALUES (?, ?, ?, ?)""",
                    [(user_id, k, u, now) for k, u in items],
                )
                self.conn.commit()
        await asyncio.to_thread(_mark)

    async def prune_sent(self, days: int = 45) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        def _prune() -> int:
            cur = self._write(
                "DELETE FROM sent_articles WHERE sent_at < ?", (cutoff,)
            )
            return cur.rowcount
        return await asyncio.to_thread(_prune)

    # ------------------------------------------------------- GNews quota

    async def gnews_used_today(self) -> int:
        rows = await asyncio.to_thread(
            self._read, "SELECT gnews_calls FROM api_usage WHERE day = ?",
            (date.today().isoformat(),),
        )
        return rows[0]["gnews_calls"] if rows else 0

    async def claim_gnews_call(self, quota: int) -> bool:
        """Reserve one GNews request for today. False means the quota is spent."""
        def _claim() -> bool:
            today = date.today().isoformat()
            with self._lock:
                self.conn.execute(
                    "INSERT OR IGNORE INTO api_usage (day, gnews_calls) VALUES (?, 0)",
                    (today,),
                )
                cur = self.conn.execute(
                    """UPDATE api_usage SET gnews_calls = gnews_calls + 1
                       WHERE day = ? AND gnews_calls < ?""",
                    (today, quota),
                )
                self.conn.commit()
                return cur.rowcount > 0
        return await asyncio.to_thread(_claim)

    async def release_gnews_call(self) -> None:
        """Give a reserved request back when the call never actually happened."""
        def _release() -> None:
            self._write(
                """UPDATE api_usage SET gnews_calls = max(0, gnews_calls - 1)
                   WHERE day = ?""",
                (date.today().isoformat(),),
            )
        await asyncio.to_thread(_release)

    async def exhaust_gnews_today(self, quota: int) -> None:
        """Mark the daily allowance as spent after the API says it is."""
        def _exhaust() -> None:
            today = date.today().isoformat()
            with self._lock:
                self.conn.execute(
                    "INSERT OR IGNORE INTO api_usage (day, gnews_calls) VALUES (?, 0)",
                    (today,),
                )
                self.conn.execute(
                    "UPDATE api_usage SET gnews_calls = ? WHERE day = ?",
                    (quota, today),
                )
                self.conn.commit()
        await asyncio.to_thread(_exhaust)

    # ------------------------------------------------------------- owner

    # Orbital serves one person. The owner is whoever claimed the install
    # first (an admin_users row nobody added), else the oldest user - which
    # covers databases from the Telegram days. Tables that held friends,
    # access requests and chats are left alone in old databases, unused.

    async def owner_id(self) -> int | None:
        rows = await asyncio.to_thread(
            self._read,
            "SELECT user_id FROM admin_users WHERE added_by IS NULL "
            "ORDER BY added_at, user_id LIMIT 1", ())
        if rows:
            return int(rows[0][0])
        rows = await asyncio.to_thread(
            self._read, "SELECT user_id FROM users ORDER BY created_at, user_id "
            "LIMIT 1", ())
        return int(rows[0][0]) if rows else None

    async def owner(self) -> User | None:
        user_id = await self.owner_id()
        return await self.get_user(user_id) if user_id is not None else None

    async def ensure_owner(self, default_digest_time: str = "08:00",
                           name: str | None = None) -> User:
        """The owner, created on a fresh install. A new owner gets id 1."""
        user_id = await self.owner_id()
        if user_id is None:
            user_id = 1
            await asyncio.to_thread(
                self._write,
                "INSERT OR IGNORE INTO admin_users (user_id, added_by, added_at) "
                "VALUES (?, NULL, ?)", (user_id, _utcnow()))
        user = await self.get_user(user_id)
        if user is None:
            user = await self.ensure_user(user_id, None, name, default_digest_time)
        elif name and name != user.first_name:
            await asyncio.to_thread(
                self._write, "UPDATE users SET first_name = ? WHERE user_id = ?",
                (name, user_id))
            user = await self.get_user(user_id)
        return user

    # ------------------------------------------------------ launch alerts

    async def set_launch_alerts(self, user_id: int, enabled: bool) -> None:
        if enabled:
            await asyncio.to_thread(
                self._write,
                "INSERT OR IGNORE INTO launch_alerts (user_id, enabled_at) "
                "VALUES (?, ?)", (user_id, _utcnow()))
        else:
            await asyncio.to_thread(
                self._write, "DELETE FROM launch_alerts WHERE user_id = ?",
                (user_id,))

    async def launch_alerts_enabled(self, user_id: int) -> bool:
        rows = await asyncio.to_thread(
            self._read, "SELECT 1 FROM launch_alerts WHERE user_id = ?",
            (user_id,))
        return bool(rows)

    async def launch_alert_users(self) -> list[tuple[int, str | None]]:
        """(user_id, timezone) for everyone who wants launch reminders."""
        rows = await asyncio.to_thread(
            self._read,
            "SELECT a.user_id, u.timezone FROM launch_alerts a "
            "LEFT JOIN users u ON u.user_id = a.user_id")
        return [(r[0], r[1]) for r in rows]

    async def claim_launch_alert(self, user_id: int, key: str) -> bool:
        """True exactly once per (user, reminder) - the caller sends only
        then, so a restart or an overlapping run can't double-send."""
        cur = await asyncio.to_thread(
            self._write,
            "INSERT OR IGNORE INTO launch_alerts_sent (user_id, alert_key, "
            "sent_at) VALUES (?, ?, ?)", (user_id, key, _utcnow()))
        return cur.rowcount == 1

    async def prune_launch_alerts(self, days: int = 60) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await asyncio.to_thread(
            self._write, "DELETE FROM launch_alerts_sent WHERE sent_at < ?",
            (cutoff,))
        return cur.rowcount

    # ----------------------------------------------------------- reports

    async def report_deliveries(self) -> set[tuple[str, int]]:
        """Every (file_id, chat_id) already sent or skipped. One row a day
        per recipient, so reading the lot stays cheap for years."""
        rows = await asyncio.to_thread(
            self._read, "SELECT file_id, chat_id FROM report_deliveries", ())
        return {(r["file_id"], r["chat_id"]) for r in rows}

    async def claim_report(self, file_id: str, chat_id: int, name: str,
                           status: str = "sent") -> bool:
        """True exactly once per (report, recipient); send only then."""
        cur = await asyncio.to_thread(
            self._write,
            "INSERT OR IGNORE INTO report_deliveries (file_id, chat_id, name, "
            "status, at) VALUES (?, ?, ?, ?, ?)",
            (file_id, chat_id, name, status, _utcnow()))
        return cur.rowcount == 1

    async def release_report(self, file_id: str, chat_id: int) -> None:
        """Undo a claim whose send failed, so the next poll tries again."""
        await asyncio.to_thread(
            self._write,
            "DELETE FROM report_deliveries WHERE file_id = ? AND chat_id = ?",
            (file_id, chat_id))

    async def report_ids(self) -> set[str]:
        rows = await asyncio.to_thread(
            self._read, "SELECT file_id FROM reports", ())
        return {r["file_id"] for r in rows}

    async def save_report(self, file_id: str, name: str, created_at: str,
                          body: str) -> None:
        await asyncio.to_thread(
            self._write,
            "INSERT OR REPLACE INTO reports (file_id, name, created_at, body) "
            "VALUES (?, ?, ?, ?)", (file_id, name, created_at, body))

    async def get_report(self, file_id: str) -> dict | None:
        rows = await asyncio.to_thread(
            self._read, "SELECT file_id, name, created_at, body FROM reports "
            "WHERE file_id = ?", (file_id,))
        return dict(rows[0]) if rows else None

    async def list_reports(self, limit: int = 60) -> list[dict]:
        """Newest first. created_at is ISO UTC, so text order is time order."""
        rows = await asyncio.to_thread(
            self._read, "SELECT file_id, name, created_at, body FROM reports "
            "ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- app

    async def create_app_token(self, user_id: int, token: str) -> None:
        await asyncio.to_thread(
            self._write, "INSERT INTO app_tokens (token, user_id, created_at) "
            "VALUES (?, ?, ?)", (token, user_id, _utcnow()))

    async def app_token_user(self, token: str) -> int | None:
        if not token:
            return None
        rows = await asyncio.to_thread(
            self._read, "SELECT user_id FROM app_tokens WHERE token = ?", (token,))
        if not rows:
            return None
        await asyncio.to_thread(
            self._write, "UPDATE app_tokens SET last_used = ? WHERE token = ?",
            (_utcnow(), token))
        return int(rows[0][0])

    async def has_app_tokens(self, user_id: int) -> bool:
        rows = await asyncio.to_thread(
            self._read, "SELECT 1 FROM app_tokens WHERE user_id = ? LIMIT 1", (user_id,))
        return bool(rows)

    async def revoke_app_tokens(self, user_id: int) -> int:
        cur = await asyncio.to_thread(
            self._write, "DELETE FROM app_tokens WHERE user_id = ?", (user_id,))
        return cur.rowcount

    async def app_prefs(self, user_id: int) -> dict:
        rows = await asyncio.to_thread(
            self._read, "SELECT * FROM app_prefs WHERE user_id = ?", (user_id,))
        prefs = {k: True for k in PREF_KEYS}
        if rows:
            prefs.update({k: bool(rows[0][k]) for k in PREF_KEYS if k in rows[0].keys()})
        return prefs

    async def set_app_pref(self, user_id: int, key: str, value: bool) -> None:
        if key not in PREF_KEYS:
            raise ValueError(key)
        await asyncio.to_thread(
            self._write, "INSERT OR IGNORE INTO app_prefs (user_id) VALUES (?)",
            (user_id,))
        await asyncio.to_thread(
            self._write, f"UPDATE app_prefs SET {key} = ? WHERE user_id = ?",
            (int(bool(value)), user_id))

    async def add_push_sub(self, user_id: int, endpoint: str, p256dh: str,
                           auth: str) -> None:
        await asyncio.to_thread(
            self._write,
            "INSERT OR REPLACE INTO push_subs (endpoint, user_id, p256dh, auth, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (endpoint, user_id, p256dh, auth, _utcnow()))

    async def remove_push_sub(self, endpoint: str) -> None:
        await asyncio.to_thread(
            self._write, "DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))

    async def push_subs(self, user_id: int) -> list[dict]:
        rows = await asyncio.to_thread(
            self._read, "SELECT endpoint, p256dh, auth FROM push_subs "
            "WHERE user_id = ?", (user_id,))
        return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}}
                for r in rows]

    async def save_briefing(self, user_id: int, items: list[dict]) -> None:
        def _save() -> None:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO briefings (user_id, created_at, items) "
                    "VALUES (?, ?, ?)", (user_id, _utcnow(), json.dumps(items)))
                self.conn.execute(
                    "DELETE FROM briefings WHERE user_id = ? AND id NOT IN "
                    "(SELECT id FROM briefings WHERE user_id = ? "
                    "ORDER BY id DESC LIMIT 14)", (user_id, user_id))
                self.conn.commit()
        await asyncio.to_thread(_save)

    async def briefings(self, user_id: int, limit: int = 7) -> list[dict]:
        rows = await asyncio.to_thread(
            self._read, "SELECT created_at, items FROM briefings WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?", (user_id, limit))
        return [{"created_at": r[0], "items": json.loads(r[1])} for r in rows]

    async def stats(self, user_id: int) -> dict:
        def _stats() -> dict:
            topics = self._read(
                "SELECT COUNT(*) AS n FROM topics WHERE user_id = ?", (user_id,)
            )[0]["n"]
            sent = self._read(
                "SELECT COUNT(*) AS n FROM sent_articles WHERE user_id = ?",
                (user_id,),
            )[0]["n"]
            return {"topics": topics, "articles_sent": sent}
        out = await asyncio.to_thread(_stats)
        out["gnews_used_today"] = await self.gnews_used_today()
        return out


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        timezone=row["timezone"],
        digest_time=row["digest_time"],
        digest_enabled=bool(row["digest_enabled"]),
        onboarded=bool(row["onboarded"]),
    )
