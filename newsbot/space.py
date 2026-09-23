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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    # Vehicle facts for the globe's launch card: size, capacity, record.
    rocket_info: dict = field(default_factory=dict)

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

    stream = pick_stream(raw.get("vid_urls") or raw.get("vidURLs") or [],
                         provider.get("name", ""))
    info = pick_info(raw, provider, rocket)

    image = raw.get("image")
    if isinstance(image, dict):
        image = image.get("image_url") or image.get("thumbnail_url")
    if not isinstance(image, str):
        image = ""

    name = str(raw.get("name") or "Unnamed launch")
    return Launch(
        rocket_info=rocket_facts(rocket),
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
        info_url=info,
        image=image,
    )


def _http(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _links(items) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            item = {"url": item}
        if isinstance(item, dict) and _http(item.get("url")):
            out.append(item)
    return out


def _is_youtube(url: str) -> bool:
    return any(h in url for h in ("youtube.com/", "youtu.be/"))


def pick_stream(vids, provider_name: str = "") -> str:
    """The livestream to open: YouTube first (it's what John asked for and
    what opens in-app on a phone), the provider's own channel before
    re-streamers, and a stream marked live before a placeholder."""
    provider = (provider_name or "").lower().split()[0] if provider_name else ""

    def score(v: dict) -> tuple:
        url = v["url"]
        who = " ".join(str(v.get(k) or "") for k in ("publisher", "source", "title")).lower()
        return (
            _is_youtube(url),
            bool(provider) and provider in who,
            bool(v.get("live")),
            -(v.get("priority") if isinstance(v.get("priority"), int) else 99),
        )

    vids = _links(vids)
    return max(vids, key=score)["url"] if vids else ""


def pick_info(raw: dict, provider: dict, rocket: dict) -> str:
    """Where the launch is actually described, before a stream exists: the
    launch's own info links (SpaceX's mission page for a SpaceX launch), then
    the provider's site, then Space Launch Now's page for this launch."""
    infos = _links(raw.get("info_urls") or raw.get("infoURLs"))
    if infos:
        provider_name = (provider.get("name") or "").lower().split()
        own = [i for i in infos if provider_name and provider_name[0] in i["url"].lower()]
        return (own or infos)[0]["url"]
    mission = raw.get("mission") or {}
    for item in _links(mission.get("info_urls")):
        return item["url"]
    slug = raw.get("slug")
    if isinstance(slug, str) and slug:
        return f"https://spacelaunchnow.me/launch/{slug}/"
    for url in (provider.get("info_url"), provider.get("wiki_url"), rocket.get("wiki_url")):
        if _http(url):
            return url
    return "https://spacelaunchnow.me/launch/"


def rocket_facts(conf: dict) -> dict:
    """The parts of a Launch Library launcher configuration worth showing.
    Every field is optional - the free tier sometimes returns a slim record."""
    if not isinstance(conf, dict):
        return {}
    image = conf.get("image")
    if isinstance(image, dict):
        image = image.get("image_url") or image.get("thumbnail_url")
    maker = conf.get("manufacturer") or {}
    out = {
        "family": conf.get("family") if isinstance(conf.get("family"), str) else "",
        "manufacturer": maker.get("name", "") if isinstance(maker, dict) else "",
        "description": conf.get("description") or "",
        "length_m": _num(conf.get("length")),
        "diameter_m": _num(conf.get("diameter")),
        "launch_mass_t": _num(conf.get("launch_mass")),
        "leo_capacity_kg": _num(conf.get("leo_capacity")),
        "gto_capacity_kg": _num(conf.get("gto_capacity")),
        "thrust_kn": _num(conf.get("to_thrust")),
        "reusable": conf.get("reusable") if isinstance(conf.get("reusable"), bool) else None,
        "maiden_flight": conf.get("maiden_flight") or "",
        "launches": conf.get("total_launch_count"),
        "successes": conf.get("successful_launches"),
        "failures": conf.get("failed_launches"),
        "wiki_url": conf.get("wiki_url") or "",
        "image": image if isinstance(image, str) else "",
    }
    return {k: v for k, v in out.items() if v not in ("", None)}


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


# ------------------------------------------------------------- calendar

def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def launches_ics(launches: list[Launch], globe_url: str = "",
                 now: datetime | None = None, alarm_minutes: int = 30) -> bytes:
    """An iCalendar feed. Each launch keeps its UID, so a calendar app that
    re-fetches the feed moves a slipped launch instead of duplicating it."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//newsbot//launches//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Rocket launches",
             "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for launch in launches:
        start = launch.net_dt
        end = start + timedelta(hours=1)
        where = ", ".join(p for p in (launch.pad, launch.location) if p)
        desc = "\n".join(p for p in (
            f"{launch.rocket} - {launch.provider}".strip(" -"),
            f"Status: {launch.status_name or launch.status}" if launch.status else "",
            f"Watch: {launch.stream_url}" if launch.stream_url else "",
            f"Info: {launch.info_url}",
            f"Globe: {globe_url}/#launch={launch.id}" if globe_url else "",
            "", launch.mission) if p is not None)
        tentative = launch.status not in REMIND_STATUSES
        lines += [
            "BEGIN:VEVENT",
            f"UID:{launch.id}@newsbot-launches",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{_ics_escape(('(TBD) ' if tentative else '') + '🚀 ' + launch.name)}",
            f"LOCATION:{_ics_escape(where)}",
            f"DESCRIPTION:{_ics_escape(desc.strip())}",
            f"URL:{launch.link}",
            f"STATUS:{'TENTATIVE' if tentative else 'CONFIRMED'}",
            "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(launch.name)}",
            f"TRIGGER:-PT{int(alarm_minutes)}M", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_ics_fold(ln) for ln in lines) + "\r\n").encode("utf-8")


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
             f'{"Watch live" if launch.stream_url else "Launch page"}</a>']
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
                                globe_url: str, app=None) -> int:
    """Telegram and/or app push, per the user's app preferences. The claim
    is per user and reminder, so neither channel can double-send."""
    users = await db.launch_alert_users()
    if not users or not space.launches:
        return 0
    now = datetime.now(timezone.utc)
    sent = 0
    for launch, lead in due_reminders(space.launches, now, leads):
        key = alert_key(launch, lead)
        minutes = (launch.net_dt - now).total_seconds() / 60
        for user_id, tz_name in users:
            if not await db.claim_launch_alert(user_id, key):
                continue        # already sent this one
            if app is not None:
                try:
                    if await app.launch_push(user_id, launch, lead, when_text(minutes)):
                        sent += 1
                    if not await app.launch_telegram(user_id):
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("Launch push to %s failed: %s", user_id, describe(exc))
            try:
                await bot.send_message(
                    user_id, launch_html(launch, tz_name, globe_url, now),
                    parse_mode="HTML", disable_web_page_preview=True)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one user must not stop the rest
                log.warning("Launch reminder to %s failed: %s", user_id,
                            describe(exc))
    return sent
