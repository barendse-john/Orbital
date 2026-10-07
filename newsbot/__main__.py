"""Entry point: python -m newsbot [--config config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys

from telegram import BotCommand
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from .ai import build_backend
from .ai.base import describe
from .config import Config, ConfigError
from .db import Database
from .digest import DigestService
from .handlers import BotHandlers
from .news import NewsFetcher
from .scheduler import DigestScheduler
from .space import SpaceService, send_launch_reminders
from .appservice import AppService
from .engine import NewsEngine
from .push import Pusher
from .reports import DriveError, ReportRelay, build_relay
from .webapp import Ctx, NewsProxy, start_web

log = logging.getLogger("newsbot")


class RetryingRequest(HTTPXRequest):
    """Retry a Telegram call that failed on the network, not on its answer.

    The Pi's line drops the occasional request, and a lost send is what the
    user experiences as the bot ignoring them. Only NetworkError (which
    TimedOut inherits) is retried - a BadRequest or Forbidden is an answer,
    and asking again would just get the same one.

    The trade-off is honest: if a send actually arrived and only its response
    was lost, the retry duplicates the message. Telegram offers no idempotency
    key, and a rare doubled message beats a regularly missing one.
    """

    RETRY_DELAYS = (0.5, 2.0)

    async def do_request(self, *args, **kwargs):
        for delay in self.RETRY_DELAYS:
            try:
                return await super().do_request(*args, **kwargs)
            except BadRequest:
                raise
            except NetworkError as exc:
                log.warning("Telegram call failed (%s); retrying in %.1fs",
                            exc, delay)
                await asyncio.sleep(delay)
        return await super().do_request(*args, **kwargs)

# Telegram's command menu. A command still works if it is missing here, but
# nobody discovers it - /retune, /requests and /users were invisible for
# exactly that reason.
COMMANDS = [
    BotCommand("topics", "What you're following"),
    BotCommand("add", "Follow a topic"),
    BotCommand("retune", "Narrow what a topic searches for"),
    BotCommand("remove", "Stop following a topic"),
    BotCommand("search", "Search the news now"),
    BotCommand("time", "Set your digest time"),
    BotCommand("timezone", "Set your timezone"),
    BotCommand("pause", "Mute the daily digest"),
    BotCommand("resume", "Unmute the daily digest"),
    BotCommand("launches", "Upcoming rocket launches"),
    BotCommand("launchalerts", "Launch reminders on/off"),
    BotCommand("app", "Open the phone app"),
    BotCommand("status", "Your settings"),
    BotCommand("users", "Who can use the bot (admins)"),
    BotCommand("requests", "Who has asked to join (admins)"),
    BotCommand("digest", "Send the digest now (admins)"),
    BotCommand("help", "How to talk to me"),
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def build_application(cfg: Config) -> Application:
    db = Database(cfg.database_path)
    db.connect()

    ai = build_backend(cfg.ai)
    fetcher = NewsFetcher(cfg.news, db)
    digest = DigestService(db, fetcher, ai, cfg)
    space = (SpaceService(cfg.space, cfg.database_path.parent / "space_cache.json")
             if cfg.space.enabled else None)
    globe_url = globe_url_for(cfg) if space else ""
    appsvc = None
    if space is not None:
        appsvc = AppService(db, ai, digest, Pusher(cfg.database_path.parent), globe_url)
        digest.app = appsvc
    engine = None
    if cfg.news.engine.enabled:
        engine = NewsEngine(db, fetcher.rss, ai, cfg, app=appsvc)
        digest.engine = engine
        if appsvc is not None:
            appsvc.engine = engine
    relay, why_not = build_relay(cfg, db)
    if relay is None and cfg.reports.folder_id:
        log.warning("Reports from Drive are off: %s", why_not)
    web = {}

    async def post_init(app: Application) -> None:
        scheduler = DigestScheduler(app, db, digest)
        handlers = BotHandlers(cfg, db, ai, fetcher, digest, scheduler,
                               space=space, globe_url=globe_url, appsvc=appsvc)
        if appsvc is not None:
            appsvc.scheduler = scheduler
        handlers.register(app)
        app.bot_data.update(
            {"db": db, "ai": ai, "fetcher": fetcher, "digest": digest,
             "scheduler": scheduler, "config": cfg}
        )
        await scheduler.reschedule_all()
        scheduler.schedule_maintenance()
        if engine is not None:
            engine.bot = app.bot

            async def engine_job(_ctx) -> None:
                try:
                    await engine.tick()
                except Exception:  # noqa: BLE001 - a bad hour must not unschedule the job
                    log.exception("News engine tick failed")

            app.job_queue.run_repeating(
                engine_job, interval=cfg.news.engine.collect_every_minutes * 60,
                first=90, name="news-engine")
        if relay is not None:
            schedule_reports(app, relay, cfg.reports.poll_minutes)
        me = await app.bot.get_me()
        if space is not None:
            schedule_space(app, db, space, cfg, globe_url, appsvc)
            loop = asyncio.get_running_loop()
            news = NewsProxy(loop, fetcher.rss)
            ctx = Ctx(space, news, appsvc, loop, globe_url, me.username or "")
            web["server"] = start_web(space, cfg.space.web_host,
                                      cfg.space.web_port, news, ctx)
        try:
            await app.bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not register the command menu: %s", exc)
        log.info("Running as @%s", me.username)

    async def post_shutdown(_app: Application) -> None:
        if web.get("server"):
            web["server"].shutdown()
            web["server"].server_close()
        if space is not None:
            await space.close()
        if engine is not None:
            await engine.close()
        if relay is not None:
            await relay.close()
        await fetcher.close()
        await ai.close()
        db.close()
        log.info("Shut down cleanly")

    # PTB's defaults are about five seconds. A domestic connection to
    # Telegram is not reliably that quick, and a timeout here surfaces as the
    # bot ignoring you.
    request = RetryingRequest(connect_timeout=15.0, read_timeout=25.0,
                              write_timeout=25.0, pool_timeout=5.0)
    getter = HTTPXRequest(connect_timeout=15.0, read_timeout=25.0,
                          write_timeout=25.0, pool_timeout=5.0)

    return (
        ApplicationBuilder()
        .token(cfg.telegram.token)
        .request(request)
        .get_updates_request(getter)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def globe_url_for(cfg: Config) -> str:
    if cfg.space.public_url:
        return cfg.space.public_url
    return f"http://{socket.gethostname()}.local:{cfg.space.web_port}"


def schedule_space(app: Application, db: Database, space: SpaceService,
                   cfg: Config, globe_url: str, appsvc=None) -> None:
    """Launch refresh, TLE refresh and the reminder check, all on PTB's job
    queue. Each callback swallows its own errors: a failed fetch must never
    unschedule the job."""
    jq = app.job_queue

    async def launches_job(_ctx) -> None:
        await space.refresh_launches()

    async def satellites_job(_ctx) -> None:
        await space.refresh_satellites()

    async def reminders_job(ctx) -> None:
        try:
            sent = await send_launch_reminders(
                ctx.bot, db, space, cfg.space.remind_before_minutes, globe_url,
                app=appsvc)
            if sent:
                log.info("Sent %d launch reminder(s)", sent)
        except Exception:  # noqa: BLE001
            log.exception("Launch reminder check failed")

    async def prune_job(_ctx) -> None:
        await db.prune_launch_alerts()

    # Straight after a restart only if the cached copy is stale - the free
    # Launch Library tier is 15 requests an hour.
    jq.run_repeating(launches_job, interval=cfg.space.launch_refresh_minutes * 60,
                     first=5 if space.launches_stale else
                     cfg.space.launch_refresh_minutes * 60,
                     name="space-launches")
    jq.run_repeating(satellites_job, interval=3600, first=20,
                     name="space-satellites")
    jq.run_repeating(reminders_job, interval=120, first=45,
                     name="space-reminders")
    jq.run_repeating(prune_job, interval=86400, first=3600, name="space-prune")


def schedule_reports(app: Application, relay: ReportRelay, minutes: int) -> None:
    """Poll the Drive folder. The first check is a minute after start, so a
    deploy delivers a report that is waiting rather than sitting on it."""

    async def reports_job(ctx) -> None:
        try:
            sent = await relay.poll(ctx.bot)
            if sent:
                log.info("Delivered %d report(s) from Drive", sent)
        except DriveError as exc:
            log.warning("Report check failed: %s", exc)
        except Exception:  # noqa: BLE001 - a bad poll must not unschedule the job
            log.exception("Report check failed")

    app.job_queue.run_repeating(reports_job, interval=minutes * 60, first=60,
                                name="reports")
    log.info("Reports from Drive: checking every %d min", minutes)


async def _check(cfg: Config) -> int:
    """Validate the setup without starting the bot."""
    ok = True
    db = Database(cfg.database_path)
    db.connect()
    print(f"  database   OK  ({cfg.database_path})")

    ai = build_backend(cfg.ai)
    healthy = await ai.health()
    print(f"  ai         {'OK ' if healthy else 'FAIL'} ({cfg.ai.backend})")
    ok &= healthy

    fetcher = NewsFetcher(cfg.news, db)
    articles, source = await fetcher.search("technology", limit=2)
    print(f"  news       {'OK ' if articles else 'FAIL'} "
          f"({len(articles)} article(s) via {source})")
    ok &= bool(articles)

    if cfg.space.enabled:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            space = SpaceService(cfg.space, Path(tmp) / "c.json")
            got = await space.refresh_launches()
            print(f"  launches   {'OK ' if got else 'FAIL'} "
                  f"({len(space.launches)} upcoming via Launch Library 2)")
            n = await space.refresh_satellites(force=True)
            count = sum(len(v) for v in space.satellites.values())
            print(f"  orbits     {'OK ' if n else 'FAIL'} "
                  f"({count} satellites via CelesTrak)")
            await space.close()
        print(f"  globe      {globe_url_for(cfg)}")

    if cfg.reports.enabled and cfg.reports.folder_id:
        relay, why_not = build_relay(cfg, db)
        if relay is None:
            print(f"  reports    FAIL ({why_not})")
            ok = False
        else:
            try:
                found = await relay.drive.list_reports(cfg.reports.folder_id)
                to = await relay.recipients()
                print(f"  reports    OK  ({len(found)} in the Drive folder; "
                      f"sent to {', '.join(map(str, to)) or 'nobody yet'})")
            except Exception as exc:  # noqa: BLE001
                print(f"  reports    FAIL ({describe(exc)})")
                ok = False
            await relay.close()
    else:
        print("  reports    off ("
              + ("no GDRIVE_REPORTS_FOLDER_ID" if cfg.reports.enabled
                 else "reports.enabled is false") + ")")

    app = ApplicationBuilder().token(cfg.telegram.token).build()
    try:
        async with app.bot:
            me = await app.bot.get_me()
        print(f"  telegram   OK  (@{me.username})")
    except Exception as exc:  # noqa: BLE001
        print(f"  telegram   FAIL ({exc})")
        ok = False

    await fetcher.close()
    await ai.close()
    db.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsbot")
    parser.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--check", action="store_true",
                        help="verify config, keys and news sources, then exit")
    args = parser.parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_level)

    if args.check:
        print("Checking setup...")
        return asyncio.run(_check(cfg))

    app = build_application(cfg)
    log.info("Starting news bot (AI: %s, news: %s)", cfg.ai.backend,
             "gnews+rss" if cfg.news.gnews.api_key else "rss")
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
