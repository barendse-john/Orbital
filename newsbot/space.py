"""Rocket launches and satellite orbits.

Feeds two things: launch reminders in Telegram, and the globe web app
(`webapp.py`). All fetching happens here, on the bot's event loop, on a
schedule - the web server only ever serves the cached snapshot, so opening the
globe ten times never costs an API call.

Sources, both free and keyless:
- The Space Devs' Launch Library 2 for upcoming launches. The free tier allows
  15 requests an hour, so launches refresh every `launch_refresh_minutes`.
- CelesTrak for orbital elements (TLEs). CelesTrak asks clients not to fetch a
  group more than once every two hours; we refresh every
  `satellite_refresh_hours`. The browser propagates the orbits (SGP4).

The cache is written to disk, so a restart serves the globe immediately and
doesn't spend the hourly allowance again.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

from .ai.base import describe
from .formatting import esc

log = logging.getLogger(__name__)

LL2_URL = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/"
CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
USER_AGENT = "newsbot-pi/1.0 (personal Telegram bot)"

# Only remind about launches that are actually expected to fly at that time.
# "TBD" launches move by days; a reminder for one is noise.
REMIND_STATUSES = {"Go", "TBC"}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Launch:
    id: str
    name: str
    net: str            # ISO 8601, UTC
    status: str         # Go / TBC / TBD / Hold ...
    status_name: str
    provider: str
    rocket: str
    mission: str
    orbit: str
    pad: str
    location: str
    lat: float
    lon: float
    stream_url: str
    info_url: str
    image: str

    @property
    def net_dt(self) -> datetime:
        return parse_time(self.net) or datetime.max.replace(tzinfo=timezone.utc)

    @property
    def link(self) -> str:
        """The one link worth tapping: the livestream if there is one."""
        return self.stream_url or self.info_url


def parse_launch(raw: dict) -> Launch | None:
    """One Launch Library 2 result -> Launch, or None if it can't be placed
    on the globe (no pad coordinates) or in time (no NET)."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    pad = raw.get("pad") or {}
    lat, lon = _num(pad.get("latitude")), _num(pad.get("longitude"))
    net = parse_time(raw.get("net"))
    if lat is None or lon is None or net is None:
        return None

    status = raw.get("status") or {}
    rocket = (raw.get("rocket") or {}).get("configuration") or {}
    mission = raw.get("mission") or {}
    orbit = mission.get("orbit") or {}
    location = pad.get("location") or {}
    provider = raw.get("launch_service_provider") or {}

    stream = ""
    for vid in raw.get("vid_urls") or raw.get("vidURLs") or []:
        url = vid.get("url") if isinstance(vid, dict) else vid
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            stream = url
            break

    image = raw.get("image")
    if isinstance(image, dict):
        image = image.get("image_url") or image.get("thumbnail_url")
    if not isinstance(image, str):
        image = ""

    name = str(raw.get("name") or "Unnamed launch")
    return Launch(
        id=str(raw["id"]),
        name=name,
        net=net.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        status=str(status.get("abbrev") or ""),
        status_name=str(status.get("name") or ""),
        provider=str(provider.get("name") or ""),
        rocket=str(rocket.get("full_name") or rocket.get("name") or ""),
        mission=str(mission.get("description") or ""),
        orbit=str(orbit.get("name") or ""),
        pad=str(pad.get("name") or ""),
        location=str(location.get("name") or ""),
        lat=lat,
        lon=lon,
        stream_url=stream,
        info_url="https://www.google.com/search?q=" + quote_plus(name + " launch"),
        image=image,
    )


def parse_tle(text: str, limit: int) -> list[list[str]]:
    """CelesTrak 3-line TLE text -> [[name, line1, line2], ...]."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[list[str]] = []
    i = 0
    while i + 2 < len(lines) and len(out) < limit:
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append([name.strip(), l1, l2])
            i += 3
        else:
            i += 1          # resync on a malformed block
    return out


def due_reminders(launches: list[Launch], now: datetime,
                  leads: list[int]) -> list[tuple[Launch, int]]:
    """Which reminder is due for each launch right now.

    Only the tightest lead that applies is returned, so a bot that was down
    for a day sends the 30-minute warning, not the 30-minute AND the day-ahead
    one at once.
    """
    leads = sorted({int(m) for m in leads if int(m) > 0})
    due = []
    for launch in launches:
        if launch.status not in REMIND_STATUSES:
            continue
        minutes_left = (launch.net_dt - now).total_seconds() / 60
        if minutes_left <= 0:
            continue
        applicable = [m for m in leads if minutes_left <= m]
        if applicable:
            due.append((launch, applicable[0]))
    return due


def alert_key(launch: Launch, lead: int) -> str:
    # NET is part of the key: a launch that slips gets reminded again for
    # its new time instead of staying silent.
    return f"{launch.id}|{lead}|{launch.net}"


def when_text(minutes: float) -> str:
    minutes = max(0, round(minutes))
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


class SpaceService:
    def __init__(self, cfg, cache_path: Path):
        self.cfg = cfg
        self.cache_path = Path(cache_path)
        self.launches: list[Launch] = []
        self.launches_at = 0.0
        self.satellites: dict[str, list[list[str]]] = {}
        self.sats_at: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        # Pre-serialised for the web thread: it swaps a reference, never
        # reads a half-built list.
        self.launches_json = b'{"launches": [], "updated": 0}'
        self.sats_json = b'{"groups": {}, "updated": 0}'
        self._load_cache()

    # ------------------------------------------------------------ fetching

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=15.0),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def refresh_launches(self) -> bool:
        try:
            resp = await self.client.get(
                LL2_URL, params={"limit": 30, "mode": "detailed",
                                 "hide_recent_previous": "true"})
            if resp.status_code == 429:
                log.warning("Launch Library rate limit hit; keeping %d cached "
                            "launches", len(self.launches))
                return False
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Launch fetch failed: %s", describe(exc))
            return False

        launches = [lnch for lnch in map(parse_launch, results) if lnch]
        launches.sort(key=lambda lnch: lnch.net)
        self.launches = launches
        self.launches_at = time.time()
        self._rebuild()
        log.info("Launches refreshed: %d upcoming", len(launches))
        return True

    async def refresh_satellites(self, force: bool = False) -> int:
        """Refresh any group older than satellite_refresh_hours. Returns how
        many groups were fetched."""
        max_age = self.cfg.satellite_refresh_hours * 3600
        fetched = 0
        for group in self.cfg.satellite_groups:
            if not force and time.time() - self.sats_at.get(group, 0) < max_age:
                continue
            try:
                resp = await self.client.get(
                    CELESTRAK_URL, params={"GROUP": group, "FORMAT": "tle"})
                resp.raise_for_status()
                sats = parse_tle(resp.text, self.cfg.max_satellites_per_group)
            except httpx.HTTPError as exc:
                log.warning("TLE fetch for %s failed: %s", group, describe(exc))
                continue
            if not sats:
                log.warning("TLE fetch for %s returned no satellites", group)
                continue
            self.satellites[group] = sats
            self.sats_at[group] = time.time()
            fetched += 1
        # Groups removed from config shouldn't linger on the globe.
        for group in list(self.satellites):
            if group not in self.cfg.satellite_groups:
                del self.satellites[group]
        if fetched:
            self._rebuild()
            log.info("Satellites refreshed: %s", ", ".join(
                f"{g} {len(s)}" for g, s in self.satellites.items()))
        return fetched

    def upcoming(self, now: datetime | None = None, limit: int = 5) -> list[Launch]:
        now = now or datetime.now(timezone.utc)
        return [lnch for lnch in self.launches if lnch.net_dt > now][:limit]

    def get(self, launch_id: str) -> Launch | None:
        return next((lnch for lnch in self.launches if lnch.id == launch_id), None)

    # --------------------------------------------------------------- cache

    def _rebuild(self) -> None:
        self.launches_json = json.dumps({
            "launches": [asdict(lnch) for lnch in self.launches],
            "updated": self.launches_at,
        }).encode()
        self.sats_json = json.dumps({
            "groups": self.satellites,
            "updated": max(self.sats_at.values(), default=0),
        }).encode()
        self._save_cache()

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "launches": [asdict(lnch) for lnch in self.launches],
                "launches_at": self.launches_at,
                "satellites": self.satellites,
                "sats_at": self.sats_at,
            }), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.warning("Could not write space cache: %s", describe(exc))

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.launches = [Launch(**d) for d in data.get("launches") or []]
            self.launches_at = float(data.get("launches_at") or 0)
            self.satellites = data.get("satellites") or {}
            self.sats_at = {k: float(v) for k, v in (data.get("sats_at") or {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Ignoring unreadable space cache: %s", describe(exc))
            return
        self.launches_json = json.dumps({
            "launches": [asdict(lnch) for lnch in self.launches],
            "updated": self.launches_at}).encode()
        self.sats_json = json.dumps({
            "groups": self.satellites,
            "updated": max(self.sats_at.values(), default=0)}).encode()

    @property
    def launches_stale(self) -> bool:
        return time.time() - self.launches_at > self.cfg.launch_refresh_minutes * 60


# ------------------------------------------------------------ Telegram text

def launch_html(launch: Launch, tz_name: str | None, globe_url: str,
                now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except (KeyError, ValueError):
        tz = timezone.utc
    local = launch.net_dt.astimezone(tz)
    minutes = (launch.net_dt - now).total_seconds() / 60
    where = ", ".join(p for p in (launch.pad, launch.location) if p)
    links = [f'<a href="{esc(launch.link)}">'
             f'{"Watch live" if launch.stream_url else "Details"}</a>']
    if globe_url:
        links.append(f'<a href="{esc(globe_url)}/#launch={esc(launch.id)}">'
                     f'On the globe</a>')
    status = f" · {esc(launch.status)}" if launch.status else ""
    return (
        f"🚀 <b>{esc(launch.name)}</b>\n"
        f"{local:%a %d %b %H:%M} (in {when_text(minutes)}){status}\n"
        f"{esc(launch.provider)}{' · ' if launch.provider and where else ''}"
        f"{esc(where)}\n"
        + " · ".join(links)
    )


async def send_launch_reminders(bot, db, space: SpaceService, leads: list[int],
                                globe_url: str) -> int:
    users = await db.launch_alert_users()
    if not users or not space.launches:
        return 0
    now = datetime.now(timezone.utc)
    sent = 0
    for launch, lead in due_reminders(space.launches, now, leads):
        key = alert_key(launch, lead)
        for user_id, tz_name in users:
            if not await db.claim_launch_alert(user_id, key):
                continue        # already sent this one
            try:
                await bot.send_message(
                    user_id, launch_html(launch, tz_name, globe_url, now),
                    parse_mode="HTML", disable_web_page_preview=True)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one user must not stop the rest
                log.warning("Launch reminder to %s failed: %s", user_id,
                            describe(exc))
    return sent
