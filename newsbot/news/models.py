"""The one article shape both news sources are normalised into."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ito|ns_|CMP$|cmp$)")


def clean_url(url: str) -> str:
    """Drop tracking parameters so the same article isn't stored twice."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = [
        kv for kv in parts.query.split("&")
        if kv and not _TRACKING_PARAMS.match(kv.split("=", 1)[0])
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), ""))


def article_key(title: str) -> str:
    """Stable id for an article across sources.

    GNews and Google News RSS hand back different URLs for the same story, so
    the headline - stripped to letters and digits - is the reliable identity.
    """
    normalised = re.sub(r"[^a-z0-9]+", "", title.lower())[:120]
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:20]


@dataclass
class Article:
    title: str
    url: str
    source: str = ""
    published_at: datetime | None = None
    description: str = ""
    summary: str = ""
    key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.title = re.sub(r"\s+", " ", (self.title or "")).strip()
        self.url = clean_url(self.url or "")
        self.description = re.sub(r"<[^>]+>", " ", self.description or "")
        self.description = re.sub(r"\s+", " ", self.description).strip()
        if not self.key:
            self.key = article_key(self.title)
