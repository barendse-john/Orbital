"""Every timed job, on APScheduler's asyncio scheduler.

Two kinds:

- the owner's daily briefing, at their chosen time in their own timezone;
- background work on a fixed interval - the news engine, launches,
  satellites, launch reminders, the Drive folder - plus nightly upkeep.

Every job swallows and logs its own errors: one bad run must never
unschedule the job or take the service down.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .db import Database, User
from .digest import DigestService

log = logging.getLogger(__name__)

MAINTENANCE_JOB = "maintenance"


def job_name(user_id: int) -> str:
    return f"digest-{user_id}"


class Scheduler:
    def __init__(self, db: Database, digest: DigestService):
        self.db = db
        self.digest = digest
        self.aps = AsyncIOScheduler(
            timezone=timezone.utc,
            # A Pi that was busy or briefly asleep runs a missed job once,
            # late, rather than skipping it or running it several times.
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 15 * 60},
        )

    # ---------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Call from inside the running event loop."""
        self.aps.start()

    def shutdown(self) -> None:
        if self.aps.running:
            self.aps.shutdown(wait=False)

    # ---------------------------------------------------------- repeating

    def every(self, name: str, job: Callable[[], Awaitable[object]], *,
              seconds: float, first: float) -> None:
        """Run `job` every `seconds`, the first time `first` seconds from now."""

        async def run() -> None:
            try:
                await job()
            except Exception:  # noqa: BLE001 - a bad run must not unschedule the job
                log.exception("Job %s failed", name)

        self.aps.add_job(
            run, IntervalTrigger(seconds=seconds), id=name, name=name,
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=first))

    # ------------------------------------------------------- the briefing

    def cancel(self, user_id: int) -> None:
        if self.aps.get_job(job_name(user_id)) is not None:
            self.aps.remove_job(job_name(user_id))

    def schedule(self, user: User) -> bool:
        """(Re)schedule the daily briefing. False if it can't run yet."""
        self.cancel(user.user_id)
        if not user.digest_enabled or not user.is_ready:
            return False
        try:
            hour, minute = (int(p) for p in user.digest_time.split(":"))
            tz = ZoneInfo(user.timezone)
        except (ValueError, AttributeError, KeyError) as exc:
            log.error("Bad schedule for %s (%s %s): %s", user.user_id,
                      user.digest_time, user.timezone, exc)
            return False
        self.aps.add_job(
            self._run, CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[user.user_id], id=job_name(user.user_id),
            name=job_name(user.user_id), replace_existing=True)
        log.info("Briefing scheduled at %s %s", user.digest_time, user.timezone)
        return True

    def schedule_maintenance(self) -> None:
        self.aps.add_job(
            self._maintenance, CronTrigger(hour=3, minute=30, timezone=timezone.utc),
            id=MAINTENANCE_JOB, name=MAINTENANCE_JOB, replace_existing=True)

    # ---------------------------------------------------------- callbacks

    async def _run(self, user_id: int) -> None:
        user = await self.db.get_user(user_id)
        if user is None or not user.digest_enabled:
            log.info("Skipping the briefing for %s (paused or unknown)", user_id)
            return
        try:
            await self.digest.send_digest(user)
        except Exception:  # noqa: BLE001 - a bad briefing must not kill the job
            log.exception("Briefing failed for %s", user_id)

    async def _maintenance(self) -> None:
        try:
            removed = await self.db.prune_sent(days=45)
            if removed:
                log.info("Pruned %d old article records", removed)
        except Exception:  # noqa: BLE001
            log.exception("Nightly maintenance failed")
