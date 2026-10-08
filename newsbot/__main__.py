"""Entry point.

    python -m newsbot             run Orbital: the web app and every background job
    python -m newsbot pair        print a link (and QR code) that signs your phone in
    python -m newsbot unpair      sign every paired phone out
    python -m newsbot --check     verify config, keys and sources, then exit

Add `--config path/to/config.yaml` to any of them (default: ./config.yaml).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys

from .ai import build_backend
from .ai.base import describe
from .appservice import AppService
from .config import Config, ConfigError
from .db import Database
from .digest import DigestService
from .engine import NewsEngine
from .news import NewsFetcher
from .push import Pusher
from .reports import DriveError, ReportRelay, build_relay
from .scheduler import Scheduler
from .space import SpaceService, send_launch_reminders
from .webapp import Ctx, NewsProxy, start_web

log = logging.getLogger("newsbot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def app_url_for(cfg: Config) -> str:
    """Where the phone reaches the Pi; pairing links and push links use it."""
    if cfg.space.public_url:
        return cfg.space.public_url
    return f"http://{socket.gethostname()}.local:{cfg.space.web_port}"


# ------------------------------------------------------------------ service

async def run_service(cfg: Config) -> int:
    db = Database(cfg.database_path)
    db.connect()

    ai = build_backend(cfg.ai)
    fetcher = NewsFetcher(cfg.news, db)
    digest = DigestService(db, fetcher, ai, cfg)
    space = (SpaceService(cfg.space, cfg.database_path.parent / "space_cache.json")
             if cfg.space.enabled else None)
    app_url = app_url_for(cfg)
    appsvc = AppService(db, ai, digest, Pusher(cfg.database_path.parent), app_url)
    digest.app = appsvc
    engine = None
    if cfg.news.engine.enabled:
        engine = NewsEngine(db, fetcher.rss, ai, cfg, app=appsvc)
        digest.engine = engine
        appsvc.engine = engine
    relay, why_not = build_relay(cfg, db)
    if relay is None and cfg.reports.folder_id:
        log.warning("Reports from Drive are off: %s", why_not)
    if relay is not None:
        relay.app = appsvc
        appsvc.relay = relay

    scheduler = Scheduler(db, digest)
    appsvc.scheduler = scheduler
    owner = await db.ensure_owner(cfg.digest.default_time)

    loop = asyncio.get_running_loop()
    scheduler.start()
    if not scheduler.schedule(owner):
        log.info("No daily briefing yet: open the app once so it can set your "
                 "timezone, and add a topic")
    scheduler.schedule_maintenance()
    if engine is not None:
        scheduler.every("news-engine", engine.tick,
                        seconds=cfg.news.engine.collect_every_minutes * 60, first=90)
    if relay is not None:
        schedule_reports(scheduler, relay, cfg.reports.poll_minutes)
    if space is not None:
        schedule_space(scheduler, db, space, cfg, appsvc)

    news = NewsProxy(loop, fetcher.rss)
    server = start_web(space, cfg.space.web_host, cfg.space.web_port, news,
                       Ctx(space, news, appsvc, loop, app_url))
    if server is None:
        log.error("The web app could not start, so there is nothing to talk to")
        return 1
    if not await db.has_app_tokens(owner.user_id):
        log.info("No phone paired yet. Run `python -m newsbot pair` to sign one in")

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):   # Windows: Ctrl+C still raises
            pass
    log.info("Orbital is running at %s/app/ (AI: %s, news: %s)", app_url,
             cfg.ai.backend, "gnews+rss" if cfg.news.gnews.api_key else "rss")
    try:
        await stop.wait()
    finally:
        scheduler.shutdown()
        server.shutdown()
        server.server_close()
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
    return 0


def schedule_space(scheduler: Scheduler, db: Database, space: SpaceService,
                   cfg: Config, appsvc: AppService) -> None:
    """Launch refresh, orbit refresh, the reminder check and upkeep."""

    async def reminders() -> None:
        sent = await send_launch_reminders(db, space, cfg.space.remind_before_minutes,
                                           appsvc)
        if sent:
            log.info("Sent %d launch reminder(s)", sent)

    # Straight after a restart only if the cached copy is stale - the free
    # Launch Library tier is 15 requests an hour.
    every = cfg.space.launch_refresh_minutes * 60
    scheduler.every("space-launches", space.refresh_launches, seconds=every,
                    first=5 if space.launches_stale else every)
    scheduler.every("space-satellites", space.refresh_satellites, seconds=3600, first=20)
    scheduler.every("space-reminders", reminders, seconds=120, first=45)
    scheduler.every("space-prune", db.prune_launch_alerts, seconds=86400, first=3600)


def schedule_reports(scheduler: Scheduler, relay: ReportRelay, minutes: int) -> None:
    """Poll the Drive folder. The first check is a minute after start, so a
    deploy picks up a report that is waiting rather than sitting on it."""

    async def poll() -> None:
        try:
            sent = await relay.poll()
            if sent:
                log.info("Notified about %d new report(s) from Drive", sent)
        except DriveError as exc:
            log.warning("Report check failed: %s", exc)

    scheduler.every("reports", poll, seconds=minutes * 60, first=60)
    log.info("Reports from Drive: checking every %d min", minutes)


# ------------------------------------------------------------------ pairing

async def pair(cfg: Config, name: str | None, url: str | None) -> int:
    db = Database(cfg.database_path)
    db.connect()
    try:
        owner = await db.ensure_owner(cfg.digest.default_time, name=name)
        app = AppService(db, None, None, None, (url or app_url_for(cfg)).rstrip("/"))
        link = app.pair_link(await app.pair(owner.user_id))
    finally:
        db.close()
    print("\nOpen this link on your phone to sign Orbital in:\n")
    print(f"  {link}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(link)
        qr.print_ascii(invert=True)
        print()
    except ImportError:
        print("(pip install qrcode to get a QR code here as well)\n")
    print("Anyone with this link can read your news, so don't share it.")
    if not (url or cfg.space.public_url):
        print("If your phone can't open it, set space.public_url in config.yaml "
              "(or pass --url) to the address it reaches the Pi at.")
    return 0


async def unpair(cfg: Config) -> int:
    db = Database(cfg.database_path)
    db.connect()
    try:
        owner = await db.owner_id()
        removed = await db.revoke_app_tokens(owner) if owner is not None else 0
    finally:
        db.close()
    print(f"Signed out {removed} phone(s). Run `python -m newsbot pair` to sign one in again.")
    return 0


# -------------------------------------------------------------------- check

async def check(cfg: Config) -> int:
    """Validate the setup without starting the service."""
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
    else:
        print("  launches   off (space.enabled is false)")

    if cfg.reports.enabled and cfg.reports.folder_id:
        relay, why_not = build_relay(cfg, db)
        if relay is None:
            print(f"  reports    FAIL ({why_not})")
            ok = False
        else:
            try:
                found = await relay.drive.list_reports(cfg.reports.folder_id)
                print(f"  reports    OK  ({len(found)} in the Drive folder)")
            except Exception as exc:  # noqa: BLE001
                print(f"  reports    FAIL ({describe(exc)})")
                ok = False
            await relay.close()
    else:
        print("  reports    off ("
              + ("no GDRIVE_REPORTS_FOLDER_ID" if cfg.reports.enabled
                 else "reports.enabled is false") + ")")

    owner = await db.owner()
    paired = owner is not None and await db.has_app_tokens(owner.user_id)
    who = f"owner {owner.first_name or owner.user_id}, " if owner else ""
    print(f"  app        {app_url_for(cfg)}/app/ ({who}"
          f"{'a phone is paired' if paired else 'no phone paired yet - run pair'})")

    await fetcher.close()
    await ai.close()
    db.close()
    return 0 if ok else 1


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsbot", description="Orbital")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "pair", "unpair", "check"],
                        help="what to do (default: run)")
    parser.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--check", action="store_true",
                        help="same as the check command")
    parser.add_argument("--name", help="pair: your name, shown in the app")
    parser.add_argument("--url", help="pair: the address your phone reaches the "
                        "Pi at, if not space.public_url")
    args = parser.parse_args(argv)
    command = "check" if args.check else args.command

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if command == "pair":
        return asyncio.run(pair(cfg, args.name, args.url))
    if command == "unpair":
        return asyncio.run(unpair(cfg))
    setup_logging(cfg.log_level)
    if command == "check":
        print("Checking setup...")
        return asyncio.run(check(cfg))
    try:
        return asyncio.run(run_service(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
