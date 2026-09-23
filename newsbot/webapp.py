"""The globe web app, served from the Pi on the local network.

Stdlib only (no new dependency on the Pi): a ThreadingHTTPServer on a daemon
thread inside the bot process, so the existing systemd unit and auto-deploy
cover it. It serves static files from ./web and the snapshots SpaceService
keeps - it never fetches anything itself.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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


def make_handler(space):
    class Handler(BaseHTTPRequestHandler):
        server_version = "newsbot-globe"

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            path = urlsplit(self.path).path
            if path == "/api/launches":
                return self._send(200, space.launches_json, "application/json")
            if path == "/api/satellites":
                return self._send(200, space.sats_json, "application/json")
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


def start_web(space, host: str, port: int) -> ThreadingHTTPServer | None:
    """Start serving; returns None (and the bot carries on) if the port is
    taken, rather than taking the whole bot down over the globe."""
    try:
        server = ThreadingHTTPServer((host, port), make_handler(space))
    except OSError as exc:
        log.error("Globe web app could not start on %s:%s (%s: %s)", host, port,
                  type(exc).__name__, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="globe-web",
                     daemon=True).start()
    log.info("Globe web app on http://%s:%s", host, port)
    return server
