"""The globe and the phone app, served from the Pi.

Stdlib only: a ThreadingHTTPServer on a daemon thread inside the bot
process, so the existing systemd unit and auto-deploy cover it. Launch and
satellite data are snapshots SpaceService keeps; anything touching the
database or the news runs as a coroutine on the bot's event loop.

Routes needing a user (/api/me/...) take `Authorization: Bearer <token>`,
the token the app got from /app in Telegram. Everything else is public to
whoever can reach the Pi - home Wi-Fi or your tailnet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .appservice import AppError
from .space import launches_ics

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024

# An explicit list, not a directory lookup: nothing outside it can be
# requested, so there is no path traversal to get wrong.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/app": ("app/index.html", "text/html; charset=utf-8"),
    "/app/": ("app/index.html", "text/html; charset=utf-8"),
    "/app/sw.js": ("app/sw.js", "text/javascript; charset=utf-8"),
    "/app/manifest.webmanifest": ("app/manifest.webmanifest", "application/manifest+json"),
    "/app/icon-192.png": ("app/icon-192.png", "image/png"),
    "/app/icon-512.png": ("app/icon-512.png", "image/png"),
    "/app/icon-maskable.png": ("app/icon-maskable.png", "image/png"),
}


class NewsProxy:
    """News for whatever was clicked on the globe.

    Always Google News RSS, never GNews: clicking around the globe would burn
    the 100-a-day GNews allowance the morning digests depend on. The search
    runs on the bot's event loop (where the RSS client lives); this is called
    from a web thread, so it hands the coroutine over and waits. Results are
    cached, so spinning back to the same country costs nothing.
    """

    TTL = 900
    LIMIT = 8

    def __init__(self, loop, rss, lookback_hours: int = 72):
        self.loop = loop
        self.rss = rss
        self.lookback_hours = lookback_hours
        self._cache: dict[str, tuple[float, list]] = {}
        self._lock = threading.Lock()

    def get(self, query: str) -> list[dict]:
        query = " ".join(query.split())[:120]
        if not query or self.rss is None or self.loop is None:
            return []
        key = query.lower()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.TTL:
                return hit[1]
        coro = self.rss.search(query, limit=self.LIMIT,
                               lookback_hours=self.lookback_hours)
        articles = asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=25)
        items = [{
            "title": a.title, "url": a.url, "source": a.source,
            "published": a.published_at.isoformat() if a.published_at else "",
        } for a in articles]
        with self._lock:
            if len(self._cache) > 300:
                self._cache.clear()
            self._cache[key] = (time.time(), items)
        return items


class Ctx:
    """What the request handler needs, bundled so it can be built in tests."""

    def __init__(self, space, news=None, app=None, loop=None, globe_url: str = "",
                 bot_username: str = ""):
        self.space = space
        self.news = news
        self.app = app
        self.loop = loop
        self.globe_url = globe_url
        self.bot_username = bot_username

    def run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)


def make_handler(space, news: NewsProxy | None = None, ctx: Ctx | None = None):
    ctx = ctx or Ctx(space, news)

    class Handler(BaseHTTPRequestHandler):
        server_version = "newsbot-globe"

        # ------------------------------------------------------ plumbing

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise AppError("Too large.", 413)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise AppError("Bad JSON.") from None
            if not isinstance(data, dict):
                raise AppError("Bad JSON.")
            return data

        def _user(self) -> int:
            if ctx.app is None:
                raise AppError("App backend is off.", 503)
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            return ctx.run(ctx.app.user_for(token))

        def log_message(self, fmt: str, *args) -> None:
            log.debug("web %s - %s", self.address_string(), fmt % args)

        # -------------------------------------------------------- verbs

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            try:
                if path.startswith("/api/me") or path.startswith("/api/app"):
                    return self._app_route(method, path)
                if method != "GET":
                    return self._json(405, {"error": "Method not allowed"})
                return self._public(path, query)
            except AppError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - never let a request kill the thread quietly
                log.exception("Web request %s %s failed", method, path)
                return self._json(500, {"error": f"{type(exc).__name__}"})

        def _public(self, path: str, query: dict) -> None:
            space = ctx.space
            if path == "/api/launches":
                return self._send(200, space.launches_json, "application/json")
            if path == "/api/satellites":
                return self._send(200, space.sats_json, "application/json")
            if path == "/api/health":
                return self._json(200, {"ok": True})
            if path == "/api/news":
                q = (query.get("q") or [""])[0]
                try:
                    items = ctx.news.get(q) if ctx.news else []
                except Exception as exc:  # noqa: BLE001 - a news hiccup is a 502, not a crash
                    log.warning("Globe news for %r failed: %s: %s", q,
                                type(exc).__name__, exc)
                    return self._json(502, {"items": [], "error": "news unavailable"})
                return self._json(200, {"query": q, "items": items})
            if path == "/calendar.ics":
                body = launches_ics(space.upcoming(limit=100), ctx.globe_url)
                return self._send(200, body, "text/calendar; charset=utf-8",
                                  {"Content-Disposition": 'inline; filename="launches.ics"'})
            m = re.fullmatch(r"/calendar/([^/]+)\.ics", path)
            if m:
                launch = space.get(unquote(m.group(1)))
                if launch is None:
                    return self._send(404, b"Unknown launch", "text/plain")
                return self._send(200, launches_ics([launch], ctx.globe_url),
                                  "text/calendar; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="launch.ics"'})
            entry = STATIC_FILES.get(path)
            if entry is None:
                return self._send(404, b"Not found", "text/plain")
            name, ctype = entry
            try:
                body = (STATIC_DIR / name).read_bytes()
            except OSError:
                return self._send(404, b"Not found", "text/plain")
            extra = {"Service-Worker-Allowed": "/app/"} if name == "app/sw.js" else None
            return self._send(200, body, ctype, extra)

        def _app_route(self, method: str, path: str) -> None:
            app = ctx.app
            if path == "/api/app/config" and method == "GET":
                return self._json(200, {
                    "push": bool(app and app.pusher and app.pusher.enabled),
                    "vapid": app.pusher.public_key if app and app.pusher else "",
                    "bot": ctx.bot_username, "globe": ctx.globe_url})
            uid = self._user()
            run = ctx.run
            if path == "/api/me" and method == "GET":
                return self._json(200, run(app.me(uid)))
            if path == "/api/me/settings" and method == "POST":
                return self._json(200, run(app.update_settings(uid, self._body())))
            if path == "/api/me/briefing" and method == "GET":
                return self._json(200, {"briefings": run(app.briefings(uid))})
            if path == "/api/me/briefing/refresh" and method == "POST":
                return self._json(200, run(app.refresh_briefing(uid), timeout=180))
            if path == "/api/me/topics":
                if method == "GET":
                    return self._json(200, {"topics": run(app.topics(uid))})
                body = self._body()
                if method == "POST":
                    return self._json(200, run(app.add_topic(
                        uid, body.get("label", ""), body.get("transcript")), timeout=90))
                if method == "DELETE":
                    return self._json(200, run(app.remove_topic(uid, body.get("label", ""))))
            if path == "/api/me/push":
                body = self._body()
                if method == "POST":
                    return self._json(200, run(app.subscribe(uid, body.get("subscription"))))
                if method == "DELETE":
                    return self._json(200, run(app.unsubscribe(body.get("endpoint", ""))))
            if path == "/api/me/feed" and method == "GET":
                return self._json(200, {"stories": run(app.feed(uid))})
            if path == "/api/me/vote" and method == "POST":
                body = self._body()
                return self._json(200, run(app.vote(uid, body.get("key", ""), body.get("vote"))))
            if path == "/api/me/push/test" and method == "POST":
                return self._json(200, run(app.test_push(uid)))
            return self._json(404, {"error": "Not found"})

    return Handler


def start_web(space, host: str, port: int, news: NewsProxy | None = None,
              ctx: Ctx | None = None) -> ThreadingHTTPServer | None:
    """Start serving; returns None (and the bot carries on) if the port is
    taken, rather than taking the whole bot down over the globe."""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(space, news, ctx))
    except OSError as exc:
        log.error("Globe web app could not start on %s:%s (%s: %s)", host, port,
                  type(exc).__name__, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="globe-web",
                     daemon=True).start()
    log.info("Globe and app on http://%s:%s", host, port)
    return server
