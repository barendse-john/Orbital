"""Everything the phone app does, as plain async methods.

The web server runs in its own threads and hands these coroutines to the
bot's event loop, so the app shares the bot's database, news fetcher, AI
backend and scheduler rather than duplicating any of them.

Pairing: /app in Telegram creates a random token and replies with a link
carrying it; the app keeps the token and sends it with every request. The
token IS the login, which is why it's long and why /app unpair revokes them.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timezone

from .brain import plan_topic, plain_query, query_is_sane

log = logging.getLogger(__name__)

REFRESH_COOLDOWN = 20 * 60        # seconds between manual briefing refreshes
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AppService:
    def __init__(self, db, ai, digest, pusher, globe_url: str):
        self.db = db
        self.ai = ai
        self.digest = digest
        self.pusher = pusher
        self.globe_url = globe_url
        self.scheduler = None            # attached once the bot is running
        self.engine = None               # NewsEngine, attached in __main__
        self._last_refresh: dict[int, float] = {}

    # ---------------------------------------------------------- pairing

    async def pair(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        await self.db.create_app_token(user_id, token)
        return token

    def pair_link(self, token: str) -> str:
        return f"{self.globe_url}/app/#pair={token}"

    async def user_for(self, token: str) -> int:
        user_id = await self.db.app_token_user(token)
        if user_id is None:
            raise AppError("Not paired - send /app to the bot on Telegram.", 401)
        return user_id

    # --------------------------------------------------------- settings

    async def me(self, user_id: int) -> dict:
        user = await self.db.get_user(user_id)
        topics = await self.db.list_topics(user_id)
        return {
            "name": (user.first_name if user else "") or "",
            "timezone": user.timezone if user else None,
            "digest_time": user.digest_time if user else None,
            "digest_enabled": bool(user and user.digest_enabled),
            "launch_alerts": await self.db.launch_alerts_enabled(user_id),
            "prefs": await self.db.app_prefs(user_id),
            "push_devices": len(await self.db.push_subs(user_id)),
            "topics": [t.label for t in topics],
        }

    async def update_settings(self, user_id: int, changes: dict) -> dict:
        user = await self.db.get_user(user_id)
        if user is None:
            raise AppError("Say hi to the bot on Telegram first.", 404)
        for key, value in (changes or {}).items():
            if key == "launch_alerts":
                await self.db.set_launch_alerts(user_id, bool(value))
            elif key == "digest_enabled":
                await self.db.set_digest_enabled(user_id, bool(value))
                await self._reschedule(user_id)
            elif key == "digest_time":
                if not isinstance(value, str) or not HHMM.match(value):
                    raise AppError("Time must look like 07:30.")
                h, m = value.split(":")
                await self.db.set_digest_time(user_id, f"{int(h):02d}:{m}")
                await self._reschedule(user_id)
            elif key in ("digest_telegram", "digest_push", "launch_telegram",
                         "launch_push", "breaking_push"):
                await self.db.set_app_pref(user_id, key, bool(value))
            else:
                raise AppError(f"Unknown setting {key!r}.")
        return await self.me(user_id)

    async def _reschedule(self, user_id: int) -> None:
        if self.scheduler is not None:
            user = await self.db.get_user(user_id)
            if user:
                self.scheduler.schedule(user)

    async def wants_telegram_digest(self, user_id: int) -> bool:
        return (await self.db.app_prefs(user_id))["digest_telegram"]

    # ---------------------------------------------------------- news

    async def briefings(self, user_id: int) -> list[dict]:
        return await self.db.briefings(user_id)

    async def refresh_briefing(self, user_id: int) -> dict:
        wait = REFRESH_COOLDOWN - (time.time() - self._last_refresh.get(user_id, 0))
        if wait > 0:
            raise AppError(f"Fresh a moment ago - try again in {int(wait // 60) + 1} min.", 429)
        user = await self.db.get_user(user_id)
        if user is None or not await self.db.list_topics(user_id):
            raise AppError("Add a topic first.", 400)
        self._last_refresh[user_id] = time.time()
        self.digest.last_items.pop(user_id, None)
        await self.digest.collect(user)
        items = self.digest.last_items.pop(user_id, [])
        if items:
            await self.db.save_briefing(user_id, items)
        return {"new": len(items), "briefings": await self.db.briefings(user_id)}

    async def on_briefing(self, user_id: int, items: list[dict]) -> None:
        await self.db.save_briefing(user_id, items)
        if (await self.db.app_prefs(user_id))["digest_push"]:
            topics = sorted({i["topic"] for i in items})
            await self.push(user_id, {
                "title": f"Your briefing · {len(items)} stories",
                "body": items[0]["title"] + (f"\n+ {', '.join(topics)}" if topics else ""),
                "url": "/app/#news", "tag": "briefing"})

    async def feed(self, user_id: int) -> list[dict]:
        """Top stories right now from the hourly engine, sent or not."""
        if self.engine is None:
            return []
        from .engine import story_item
        return [story_item(s) | {"relevance": s["relevance"], "impact": s["impact"]}
                for s in (await self.engine.ranked(user_id, hours=24))[:30]]

    async def vote(self, user_id: int, key: str, vote) -> dict:
        if self.engine is None or not key:
            raise AppError("Voting needs the news engine.", 400)
        try:
            vote = int(vote)
        except (TypeError, ValueError):
            raise AppError("Vote must be 1, 0 or -1.") from None
        await self.engine.vote(user_id, key, vote)
        return {"ok": True}

    async def breaking_news(self, user_id: int, story: dict, bot=None) -> None:
        prefs = await self.db.app_prefs(user_id)
        if prefs.get("breaking_push", True):
            await self.push(user_id, {
                "title": f"⚡ {story['topic']}", "body": story["title"],
                "url": "/app/#news", "tag": f"news-{story['key'][:40]}",
                "link": story["url"]})
        if bot is not None and prefs.get("digest_telegram", True):
            from .formatting import esc
            try:
                await bot.send_message(
                    user_id, f"⚡ <b>{esc(story['topic'])}</b>\n"
                    f'<a href="{esc(story["url"])}">{esc(story["title"])}</a> - {esc(story["source"])}',
                    parse_mode="HTML", disable_web_page_preview=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Breaking news to %s on Telegram failed: %s", user_id, exc)

    # --------------------------------------------------------- topics

    async def topics(self, user_id: int) -> list[str]:
        return [t.label for t in await self.db.list_topics(user_id)]

    async def add_topic(self, user_id: int, label: str,
                        transcript: list[dict] | None = None) -> dict:
        label = " ".join((label or "").split())[:60]
        if not label:
            raise AppError("Name the topic.")
        transcript = [t for t in (transcript or []) if isinstance(t, dict)][:6]
        try:
            plan = await plan_topic(self.ai, label, transcript=transcript)
        except Exception as exc:  # noqa: BLE001 - the model being down mustn't block adding
            log.warning("Topic planning failed in app: %s: %s", type(exc).__name__, exc)
            plan = None
        if plan is not None and plan.question and not plan.query:
            return {"question": plan.question, "transcript": transcript}
        answers = " ".join(str(t.get("a", "")) for t in transcript) or None
        query = plan.query if (plan and plan.query and query_is_sane(plan.query)) \
            else plain_query(label, answers)
        final_label = (plan.label if plan and plan.label else label)[:60]
        if not await self.db.add_topic(user_id, final_label, query):
            await self.db.update_topic_query(user_id, final_label, query)
        return {"added": final_label, "topics": await self.topics(user_id)}

    async def remove_topic(self, user_id: int, label: str) -> dict:
        if not await self.db.remove_topic(user_id, label):
            raise AppError("No such topic.", 404)
        return {"topics": await self.topics(user_id)}

    # ----------------------------------------------------------- push

    async def subscribe(self, user_id: int, sub: dict) -> dict:
        keys = (sub or {}).get("keys") or {}
        endpoint = (sub or {}).get("endpoint")
        if not (isinstance(endpoint, str) and endpoint.startswith("https://")
                and keys.get("p256dh") and keys.get("auth")):
            raise AppError("Bad subscription.")
        await self.db.add_push_sub(user_id, endpoint, keys["p256dh"], keys["auth"])
        return {"ok": True}

    async def unsubscribe(self, endpoint: str) -> dict:
        await self.db.remove_push_sub(endpoint or "")
        return {"ok": True}

    async def push(self, user_id: int, payload: dict) -> int:
        subs = await self.db.push_subs(user_id)
        gone = await self.pusher.send(subs, payload) if self.pusher else []
        for endpoint in gone:
            await self.db.remove_push_sub(endpoint)
        return len(subs) - len(gone)

    async def test_push(self, user_id: int) -> dict:
        sent = await self.push(user_id, {
            "title": "Notifications work", "body": "Launch reminders and your "
            "briefing will arrive here.", "url": "/app/", "tag": "test"})
        return {"sent": sent}

    # --------------------------------------------------------- launches

    async def launch_push(self, user_id: int, launch, lead_minutes: int,
                          when_text: str) -> bool:
        if not (await self.db.app_prefs(user_id))["launch_push"]:
            return False
        live = " · stream is up" if launch.stream_url else ""
        return bool(await self.push(user_id, {
            "title": f"🚀 T-{when_text} · {launch.name}",
            "body": f"{launch.provider} · {launch.pad}{live}",
            "url": f"/app/#launch={launch.id}", "tag": f"launch-{launch.id}",
            "link": launch.link}))

    async def launch_telegram(self, user_id: int) -> bool:
        return (await self.db.app_prefs(user_id))["launch_telegram"]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
