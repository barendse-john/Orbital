"""Per-user daily digest jobs, each in that user's own timezone."""

from __future__ import annotations

import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from .db import Database, User
from .digest import DigestService

log = logging.getLogger(__name__)

MAINTENANCE_JOB = "maintenance"


def job_name(user_id: int) -> str:
    return f"digest-{user_id}"


class DigestScheduler:
    def __init__(self, app: Application, db: Database, digest: DigestService):
        self.app = app
        self.db = db
        self.digest = digest

    @property
    def job_queue(self):
        if self.app.job_queue is None:
            raise RuntimeError(
                "JobQueue missing. Install with: "
                "pip install 'python-telegram-bot[job-queue]'"
            )
        return self.app.job_queue

    # ------------------------------------------------------------------

    def cancel(self, user_id: int) -> None:
        for job in self.job_queue.get_jobs_by_name(job_name(user_id)):
            job.schedule_removal()

    def schedule(self, user: User) -> bool:
        """(Re)schedule one user's digest. False if they aren't set up yet."""
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

        self.job_queue.run_daily(
            self._run,
            time=dtime(hour=hour, minute=minute, tzinfo=tz),
            name=job_name(user.user_id),
            data={"user_id": user.user_id},
            chat_id=user.user_id,
        )
        log.info("Digest for %s scheduled at %s %s", user.user_id,
                 user.digest_time, user.timezone)
        return True

    async def reschedule_all(self) -> int:
        count = 0
        for user in await self.db.all_users():
            if self.schedule(user):
                count += 1
        log.info("%d digest job(s) scheduled", count)
        return count

    def schedule_maintenance(self) -> None:
        for job in self.job_queue.get_jobs_by_name(MAINTENANCE_JOB):
            job.schedule_removal()
        self.job_queue.run_daily(
            self._maintenance,
            time=dtime(hour=3, minute=30, tzinfo=ZoneInfo("UTC")),
            name=MAINTENANCE_JOB,
        )

    # ---------------------------------------------------------- callbacks

    async def _run(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = context.job.data["user_id"]
        user = await self.db.get_user(user_id)
        if user is None or not user.digest_enabled:
            log.info("Skipping digest for %s (paused or unknown)", user_id)
            return
        try:
            await self.digest.send_digest(context.bot, user)
        except Exception:  # noqa: BLE001 - a bad digest must not kill the job
            log.exception("Digest failed for %s", user_id)

    async def _maintenance(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        removed = await self.db.prune_sent(days=45)
        if removed:
            log.info("Pruned %d old article records", removed)
