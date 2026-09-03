"""SQLite storage. Everything survives a restart or a power cut on the Pi.

The sqlite3 calls are synchronous but tiny; each public method hops onto a
worker thread so the asyncio event loop is never blocked, and a single lock
keeps concurrent writes honest.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS allowed_users (
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
            self._conn.commit()
        log.info("Database ready at %s", self.path)

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

    async def set_onboarded(self, user_id: int, done: bool = True) -> None:
        await asyncio.to_thread(
            self._write, "UPDATE users SET onboarded = ? WHERE user_id = ?",
            (1 if done else 0, user_id),
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

    async def clear_topics(self, user_id: int) -> int:
        def _clear() -> int:
            cur = self._write("DELETE FROM topics WHERE user_id = ?", (user_id,))
            return cur.rowcount
        return await asyncio.to_thread(_clear)

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

    # ------------------------------------------------------------ access

    async def allowed_user_ids(self) -> set[int]:
        rows = await asyncio.to_thread(
            self._read, "SELECT user_id FROM allowed_users", ()
        )
        return {r["user_id"] for r in rows}

    async def allow_user(self, user_id: int, added_by: int | None = None) -> None:
        await asyncio.to_thread(
            self._write,
            """INSERT OR IGNORE INTO allowed_users (user_id, added_by, added_at)
               VALUES (?, ?, ?)""",
            (user_id, added_by, _utcnow()),
        )

    async def deny_user(self, user_id: int) -> bool:
        def _deny() -> bool:
            cur = self._write(
                "DELETE FROM allowed_users WHERE user_id = ?", (user_id,)
            )
            return cur.rowcount > 0
        return await asyncio.to_thread(_deny)

    # ------------------------------------------------------------- stats

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
