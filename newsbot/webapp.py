"""The globe web app, served from the Pi on the local network.

Stdlib only (no new dependency on the Pi): a ThreadingHTTPServer on a daemon
thread inside the bot process, so the existing systemd unit and auto-deploy
cover it. It serves static files from ./web and the snapshots SpaceService
keeps - it never fetches anything itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web"

# An explicit list, not a directory lookup: nothing outside it can be
# requested, so there is no path traversal to get wrong.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
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


def make_handler(space, news: NewsProxy | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "newsbot-globe"

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            path = urlsplit(self.path).path
            if path == "/api/launches":
                return self._send(200, space.launches_json, "application/json")
            if path == "/api/satellites":
                return self._send(200, space.sats_json, "application/json")
            if path == "/api/news":
                query = (parse_qs(urlsplit(self.path).query).get("q") or [""])[0]
                try:
                    items = news.get(query) if news else []
                except Exception as exc:  # noqa: BLE001 - a news hiccup is a 502, not a crash
                    log.warning("Globe news for %r failed: %s: %s", query,
                                type(exc).__name__, exc)
                    return self._send(502, b'{"items": [], "error": "news unavailable"}',
                                      "application/json")
                body = json.dumps({"query": query, "items": items}).encode()
                return self._send(200, body, "application/json")
            if path == "/api/health":
                return self._send(200, b'{"ok": true}', "application/json")
            entry = STATIC_FILES.get(path)
            if entry is None:
                return self._send(404, b"Not found", "text/plain")
            name, ctype = entry
            try:
                body = (STATIC_DIR / name).read_bytes()
            except OSError:
                return self._send(404, b"Not found", "text/plain")
            self._send(200, body, ctype)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt: str, *args) -> None:
            log.debug("web %s - %s", self.address_string(), fmt % args)

    return Handler


def start_web(space, host: str, port: int,
              news: NewsProxy | None = None) -> ThreadingHTTPServer | None:
    """Start serving; returns None (and the bot carries on) if the port is
    taken, rather than taking the whole bot down over the globe."""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(space, news))
    except OSError as exc:
        log.error("Globe web app could not start on %s:%s (%s: %s)", host, port,
                  type(exc).__name__, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="globe-web",
                     daemon=True).start()
    log.info("Globe web app on http://%s:%s", host, port)
    return server
