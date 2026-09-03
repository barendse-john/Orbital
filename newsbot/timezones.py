"""Turning a shared Telegram location into an IANA timezone, offline.

timezonefinder is optional: without it the bot simply asks for a city name
instead, so the install still works on a bare Raspberry Pi.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_finder = None
_unavailable = False


def _get_finder():
    global _finder, _unavailable
    if _finder is not None or _unavailable:
        return _finder
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        _unavailable = True
        log.warning(
            "timezonefinder is not installed - shared locations cannot be "
            "converted. pip install timezonefinder"
        )
        return None
    # in_memory=False keeps RAM use low, which matters on a Pi.
    _finder = TimezoneFinder()
    return _finder


def available() -> bool:
    return _get_finder() is not None


async def timezone_from_coords(lat: float, lon: float) -> str | None:
    finder = _get_finder()
    if finder is None:
        return None

    def _lookup() -> str | None:
        try:
            return finder.timezone_at(lat=lat, lng=lon) or \
                finder.certain_timezone_at(lat=lat, lng=lon)
        except Exception as exc:  # noqa: BLE001
            log.error("Timezone lookup failed for %s,%s: %s", lat, lon, exc)
            return None

    return await asyncio.to_thread(_lookup)
