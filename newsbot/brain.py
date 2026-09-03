"""All the AI prompting lives here: understanding messages, summarising
articles, turning a city name into a timezone.

Every function degrades gracefully - if the model is unreachable or answers
with nonsense, the bot falls back to keyword rules and keeps working.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, available_timezones

from .ai import AIBackend, AIError

log = logging.getLogger(__name__)

ACTIONS = {
    "add_topic", "remove_topic", "list_topics", "clear_topics", "search",
    "set_time", "set_timezone", "digest_now", "pause", "resume", "help", "chat",
}

INTENT_SYSTEM = """You are the intent parser for a personal news bot on Telegram.
Read the user's message and reply with ONE JSON object and nothing else.

Fields:
  action    one of: add_topic, remove_topic, list_topics, clear_topics, search,
            set_time, set_timezone, digest_now, pause, resume, help, chat
  topic     short human label for the topic (add_topic / remove_topic)
  query     news search query, 2-6 words, no punctuation (add_topic / search)
  time      "HH:MM" 24-hour (set_time only)
  timezone  IANA name like "Europe/Amsterdam" (set_timezone only)
  reply     a short friendly sentence to send back (always)

Guidance:
- "i like Manchester United", "follow SpaceX", "keep me posted on the ECB"
  -> add_topic. Standing interest = a saved topic.
- "search up news about satellites", "any updates on Starship?", "what's
  happening in Sudan" -> search. A one-off question = a search, not a topic.
- "stop sending me F1", "drop tennis" -> remove_topic.
- "what am i following" -> list_topics.
- "send it at 7am", "make it 19:30" -> set_time.
- "i'm in Tokyo", "i moved to Lisbon" -> set_timezone.
- "send me the digest now", "catch me up" -> digest_now.
- "pause"/"mute" -> pause. "resume"/"unmute" -> resume.
- Anything else conversational -> chat, with a warm one-line reply.
Keep query terms newsworthy: for "i like Manchester United" use
query "Manchester United". Never invent topics the user did not mention."""

SUMMARY_SYSTEM = """You write one-line news summaries for a Telegram digest.
For each numbered article you receive, write ONE sentence of at most 20 words
saying what actually happened - concrete facts, no hype, no "this article
discusses". Reply with a JSON array of strings, one per article, in the same
order. No other text."""

TIMEZONE_SYSTEM = """The user names a place. Reply with the matching IANA
timezone identifier and nothing else, e.g. "Europe/Amsterdam". If the place is
ambiguous or unknown, reply exactly "UNKNOWN"."""


@dataclass
class Intent:
    action: str = "chat"
    topic: str | None = None
    query: str | None = None
    time: str | None = None
    timezone: str | None = None
    reply: str | None = None
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# JSON helpers - small models like to wrap output in prose or code fences.
# --------------------------------------------------------------------------

def extract_json(text: str) -> dict | list | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


# --------------------------------------------------------------------------
# Intent
# --------------------------------------------------------------------------

async def parse_intent(
    ai: AIBackend | None, message: str, *, topics: list[str] | None = None,
) -> Intent:
    """Understand a free-text message. Falls back to keyword rules."""
    topics = topics or []
    if ai is not None:
        context = (
            f"Topics the user already follows: {', '.join(topics) or 'none yet'}"
        )
        try:
            raw = await ai.complete(
                INTENT_SYSTEM, f"{context}\n\nMessage: {message}", max_tokens=300
            )
            data = extract_json(raw)
            if isinstance(data, dict):
                intent = _intent_from_dict(data)
                if intent is not None:
                    return intent
            log.warning("Unparseable intent response: %s", raw[:200])
        except AIError as exc:
            log.warning("Intent parsing unavailable (%s); using keyword rules", exc)

    return fallback_intent(message)


def _intent_from_dict(data: dict) -> Intent | None:
    action = str(data.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return None
    intent = Intent(
        action=action,
        topic=_clean(data.get("topic")),
        query=_clean(data.get("query")),
        time=_clean(data.get("time")),
        timezone=_clean(data.get("timezone")),
        reply=_clean(data.get("reply")),
        raw=data,
    )
    # A topic with no query (or the reverse) is still usable.
    if intent.action == "add_topic":
        intent.query = intent.query or intent.topic
        intent.topic = intent.topic or intent.query
        if not intent.topic:
            return None
    if intent.action == "search" and not intent.query:
        intent.query = intent.topic
        if not intent.query:
            return None
    if intent.action == "set_time":
        intent.time = normalise_time(intent.time or "")
        if not intent.time:
            return None
    if intent.action == "set_timezone" and not valid_timezone(intent.timezone or ""):
        return None
    return intent


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip()
    return text or None


_ADD_RE = re.compile(
    r"^(?:/add\s+|add\s+|track\s+|follow\s+|i\s+like\s+|i'?m\s+into\s+"
    r"|keep\s+me\s+(?:posted|updated)\s+(?:on|about)\s+|subscribe\s+to\s+)(.+)$",
    re.I,
)
_REMOVE_RE = re.compile(
    r"^(?:/remove\s+|remove\s+|unfollow\s+|untrack\s+|drop\s+|forget\s+"
    r"|stop\s+(?:sending|following|tracking)?\s*(?:me\s+)?(?:about\s+)?)(.+)$",
    re.I,
)
_SEARCH_RE = re.compile(
    r"^(?:/search\s+|search\s+(?:up\s+)?(?:for\s+)?(?:news\s+(?:about|on)\s+)?"
    r"|news\s+(?:about|on)\s+|find\s+(?:news\s+(?:about|on)\s+)?"
    r"|what'?s\s+(?:happening|new)\s+(?:with|in|about)\s+|any\s+news\s+(?:on|about)\s+"
    r"|updates?\s+on\s+)(.+)$",
    re.I,
)
_TIME_RE = re.compile(
    r"\b(?:at|to|for)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I
)


def fallback_intent(message: str) -> Intent:
    """Keyword rules for when the model is unavailable."""
    text = message.strip()
    low = text.lower().rstrip("?!.")

    if low in {"/topics", "topics", "list", "my topics", "what am i following"}:
        return Intent(action="list_topics")
    if low in {"/digest", "digest", "digest now", "catch me up", "update me"}:
        return Intent(action="digest_now")
    if low in {"/pause", "pause", "mute", "stop"}:
        return Intent(action="pause")
    if low in {"/resume", "resume", "unmute", "start again"}:
        return Intent(action="resume")
    if low in {"/help", "help", "what can you do"}:
        return Intent(action="help")

    if m := _SEARCH_RE.match(text):
        q = m.group(1).strip(" ?!.")
        return Intent(action="search", query=q, topic=q)
    if m := _REMOVE_RE.match(text):
        t = m.group(1).strip(" ?!.")
        return Intent(action="remove_topic", topic=t)
    if m := _ADD_RE.match(text):
        t = m.group(1).strip(" ?!.")
        return Intent(action="add_topic", topic=t, query=t)

    if re.search(r"\b(digest|send)\b.*\b(at|time)\b", low) or low.startswith("/time"):
        if hhmm := normalise_time(text):
            return Intent(action="set_time", time=hhmm)

    return Intent(
        action="chat",
        reply=(
            "I'm not sure what you meant. Try \"follow Manchester United\", "
            "\"news about satellites\", or /help."
        ),
    )


def normalise_time(text: str) -> str | None:
    """'7am', '19:30', 'at 7', '08:00' -> 'HH:MM'."""
    text = text.strip()
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", text):
        h, mi = int(m.group(1)), int(m.group(2))
        return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None
    for m in _TIME_RE.finditer(text):
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        suffix = (m.group(3) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return None


# --------------------------------------------------------------------------
# Timezones
# --------------------------------------------------------------------------

_ALL_TZ: set[str] | None = None


def valid_timezone(name: str) -> bool:
    global _ALL_TZ
    if not name:
        return False
    if _ALL_TZ is None:
        _ALL_TZ = available_timezones()
    if name in _ALL_TZ:
        return True
    try:
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def guess_timezone_offline(place: str) -> str | None:
    """Match a place name against the IANA database without any model call."""
    global _ALL_TZ
    if _ALL_TZ is None:
        _ALL_TZ = available_timezones()
    needle = re.sub(r"[^a-z]", "", place.lower())
    if not needle:
        return None
    for zone in sorted(_ALL_TZ):
        city = re.sub(r"[^a-z]", "", zone.rsplit("/", 1)[-1].lower())
        if city == needle:
            return zone
    return None


async def resolve_timezone(ai: AIBackend | None, place: str) -> str | None:
    """City or country name -> IANA timezone."""
    if valid_timezone(place.strip()):
        return place.strip()
    if offline := guess_timezone_offline(place):
        return offline
    if ai is None:
        return None
    try:
        answer = (await ai.complete(TIMEZONE_SYSTEM, place, max_tokens=30)).strip()
    except AIError as exc:
        log.warning("Timezone lookup failed: %s", exc)
        return None
    answer = answer.strip().strip('".')
    return answer if valid_timezone(answer) else None


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------

async def summarise_articles(
    ai: AIBackend | None, articles: list, *, max_tokens_per_article: int = 60,
) -> list[str]:
    """One line per article, in order. Falls back to the article's own blurb."""
    if not articles:
        return []

    def _fallback() -> list[str]:
        out = []
        for a in articles:
            blurb = (getattr(a, "description", "") or "").strip()
            blurb = re.sub(r"<[^>]+>", "", blurb)
            blurb = re.sub(r"\s+", " ", blurb)
            if len(blurb) > 160:
                blurb = blurb[:157].rstrip() + "..."
            out.append(blurb)
        return out

    if ai is None:
        return _fallback()

    lines = []
    for i, a in enumerate(articles, 1):
        desc = re.sub(r"\s+", " ", (getattr(a, "description", "") or ""))[:400]
        lines.append(f"{i}. {a.title}\n   source: {a.source}\n   blurb: {desc}")
    prompt = "\n".join(lines)

    try:
        raw = await ai.complete(
            SUMMARY_SYSTEM, prompt,
            max_tokens=max(120, max_tokens_per_article * len(articles)),
        )
    except AIError as exc:
        log.warning("Summarisation failed (%s); using article blurbs", exc)
        return _fallback()

    data = extract_json(raw)
    if isinstance(data, list) and data:
        summaries = [str(s).strip() for s in data]
        if len(summaries) < len(articles):
            summaries += _fallback()[len(summaries):]
        return summaries[:len(articles)]

    log.warning("Unparseable summary response: %s", raw[:200])
    return _fallback()
