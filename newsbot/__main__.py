"""Entry point: python -m newsbot [--config config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from .ai import build_backend
from .config import Config, ConfigError
from .db import Database
from .digest import DigestService
from .handlers import BotHandlers
from .news import NewsFetcher
from .scheduler import DigestScheduler

log = logging.getLogger("newsbot")

COMMANDS = [
    BotCommand("topics", "What you're following"),
    BotCommand("add", "Follow a topic"),
    BotCommand("remove", "Stop following a topic"),
    BotCommand("search", "Search the news now"),
    BotCommand("digest", "Send today's digest"),
    BotCommand("time", "Set your digest time"),
    BotCommand("timezone", "Set your timezone"),
    BotCommand("pause", "Mute the daily digest"),
    BotCommand("resume", "Unmute the daily digest"),
    BotCommand("status", "Your settings"),
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

    async def post_init(app: Application) -> None:
        scheduler = DigestScheduler(app, db, digest)
        handlers = BotHandlers(cfg, db, ai, fetcher, digest, scheduler)
        handlers.register(app)
        app.bot_data.update(
            {"db": db, "ai": ai, "fetcher": fetcher, "digest": digest,
             "scheduler": scheduler, "config": cfg}
        )
        await scheduler.reschedule_all()
        scheduler.schedule_maintenance()
        try:
            await app.bot.set_my_commands(COMMANDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not register the command menu: %s", exc)
        me = await app.bot.get_me()
        log.info("Running as @%s", me.username)

    async def post_shutdown(_app: Application) -> None:
        await fetcher.close()
        await ai.close()
        db.close()
        log.info("Shut down cleanly")

    # PTB's defaults are about five seconds. A domestic connection to
    # Telegram is not reliably that quick, and a timeout here surfaces as the
    # bot ignoring you.
    request = HTTPXRequest(connect_timeout=15.0, read_timeout=25.0,
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
