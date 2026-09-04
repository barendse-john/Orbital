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
    "add_topic", "remove_topic", "retune_topic", "list_topics", "clear_topics",
    "search",
    "set_time", "set_timezone", "digest_now", "pause", "resume", "help", "chat",
}

INTENT_SYSTEM = """You are the intent parser for a personal news bot on Telegram.
Read the user's message and reply with ONE JSON object and nothing else.

Fields:
  action    one of: add_topic, remove_topic, retune_topic, list_topics,
            clear_topics, search, set_time, set_timezone, digest_now, pause,
            resume, help, chat
  topic     short human label for the topic (add_topic / remove_topic)
  query     news search query, 2-6 words, no punctuation (add_topic / search)
  time      "HH:MM" 24-hour (set_time only)
  timezone  IANA name like "Europe/Amsterdam" (set_timezone only)
  reply     one short sentence to send back (always)

Guidance:
- "i like Manchester United", "follow SpaceX", "keep me posted on the ECB"
  -> add_topic. Standing interest = a saved topic.
- "search up news about satellites", "any updates on Starship?", "what's
  happening in Sudan" -> search. A one-off question = a search, not a topic.
- "stop sending me F1", "drop tennis" -> remove_topic.
- "the space topic is too broad", "finance is giving me junk", "fix my
  finance topic" -> retune_topic, with `topic` set to which one. They
  want it narrowed, not deleted.
- "what am i following" -> list_topics.
- "send it at 7am", "make it 19:30" -> set_time.
- "i'm in Tokyo", "i moved to Lisbon" -> set_timezone.
- "send me the digest now", "catch me up" -> digest_now. This opens a
  conversation about the news rather than sending a digest; the digest
  itself only goes out in the morning.
- "pause"/"mute" -> pause. "resume"/"unmute" -> resume.
- Anything else conversational -> chat, with a one-line reply.
Keep query terms newsworthy: for "i like Manchester United" use
query "Manchester United". Never invent topics the user did not mention.

Tone for `reply`: say the thing and stop. No sign-offs, no offers of further
help, no "let me know if...", "feel free to...", "anything else?", "happy to
help", "just say the word". Do not ask a question unless you genuinely cannot
act without the answer. One sentence, warm but flat-ended - the user is
texting a tool, not being served by a concierge."""

TOPIC_SYSTEM = """You are working out what someone actually wants news about,
then writing the search query for it.

Reply with ONE JSON object and nothing else:
  question  the next thing worth asking, or null if you can write the query
  label     short human label for the topic, 1-4 words
  query     the search query, or null while you are still asking

Ask when the answers so far would still return unrelated news. "finance"
matches a local water-treatment grant and a film production company;
"Manchester United" and "Starship launches" are already specific and need no
question at all. Each question must build on what they have already said -
never re-ask something they answered, and never ask two things at once. Stop
the moment you could write a good query; a short exchange is the goal, not a
thorough one.

When you write a query:
- Syntax: "quoted phrases" for anything multi-word, OR between alternatives,
  AND to require both, NOT to exclude. Parentheses group.
- Build it from THEIR words. If they said "reusable rockets", search for
  "reusable rockets", not "space transportation".
- 2 to 8 phrases, the words a headline would actually use.
- Under 180 characters.
- Only what they asked for - never widen the topic on their behalf.

Examples:
  Interest: finance
    -> {"question": "Markets and central banks, company earnings, or crypto?",
        "label": "Finance", "query": null}
  Interest: finance
  Q: Markets and central banks, company earnings, or crypto?
  A: markets and rates, not crypto
    -> {"question": "Any particular region, or everywhere?",
        "label": "Markets and Rates", "query": null}
  Interest: finance
  Q: Markets and central banks, company earnings, or crypto?
  A: markets and rates, not crypto
  Q: Any particular region, or everywhere?
  A: europe mostly
    -> {"question": null, "label": "European Markets",
        "query": "(\"financial markets\" OR \"central bank\" OR \"interest rates\") AND (Europe OR ECB OR eurozone) NOT crypto"}
  Interest: Manchester United
    -> {"question": null, "label": "Manchester United",
        "query": "\"Manchester United\""}"""

REFINE_SYSTEM = """You adjust an existing news search query.

Reply with ONE JSON object and nothing else:
  query  the whole new query
  note   at most six words saying what changed, e.g. "added reusable rockets"

Keep everything the query already covers unless the instruction says to drop
it. Use their words. Same syntax as before: "quoted phrases", OR, AND, NOT,
parentheses. Under 180 characters."""


SUMMARY_SYSTEM = """You write one-line news summaries for a Telegram digest.
For each numbered article you receive, write ONE sentence of at most 20 words
saying what actually happened - concrete facts, no hype, no "this article
discusses". Reply with a JSON array of strings, one per article, in the same
order. No other text."""

CHAT_SYSTEM = """You are a news bot having a back-and-forth on Telegram.

Reply with ONE JSON object and nothing else:
  search    a news search query when answering needs current reporting, else null
  reply     what to say when you are NOT searching, else null
  end       true when they are winding the conversation up, else false
  steer     {"label": <one of their topics>, "change": <what to change>} when
            they say what they want more or less of in a topic they follow -
            "I don't care about satellite TV", "more on launch startups".
            null otherwise.
  interests names of companies, people, missions or subjects they asked
            about, as a list, each tagged with the topic of theirs it belongs
            to: [{"label": <their topic>, "phrase": "nvidia"}]. Only things
            that clearly sit under a topic they already follow, and only what
            they actually asked about. [] otherwise.

Search whenever they ask what is happening, what was said, what the numbers
are, or anything else that needs today's reporting. Use `reply` only for the
things a search cannot answer - a clarifying question when you truly cannot
tell what they mean, or an acknowledgement.

`end` is true for "thanks", "that's all", "bye", "never mind" - anything that
reads as closing the conversation, even a warm one.

Tone: say the thing and stop. No sign-offs, no "let me know if you need
anything", no offers of further help. One or two sentences."""

CHAT_ANSWER_SYSTEM = """You answer a question using ONLY the numbered articles
you are given.

Write two to four sentences of plain prose - what actually happened, the
numbers, who said it. After a claim, cite the article it came from as [1],
[2] and so on, matching the numbers you were given. Every article you draw on
gets a marker; never invent a number you were not given, and never write a URL.

If the articles do not answer the question, say so plainly in one sentence
instead of padding.

Tone: say the thing and stop. No sign-offs, no "let me know if you need
anything", no unprompted follow-up questions."""

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
    if intent.action == "retune_topic" and not intent.topic:
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
_RETUNE_RE = re.compile(
    r"^(?:/retune\s+|retune\s+|(?:re)?tune\s+|fix\s+(?:my\s+)?)(.+?)"
    r"(?:\s+topic)?$|^(?:the\s+|my\s+)?(.+?)\s+(?:topic\s+)?is\s+"
    r"(?:too\s+broad|giving\s+me\s+junk|rubbish|useless|wrong)\b",
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

    if m := _RETUNE_RE.match(text):
        topic = (m.group(1) or m.group(2) or "").strip(" ?!.")
        if topic:
            return Intent(action="retune_topic", topic=topic)
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
# Topics: one clarifying question, then a query worth searching
# --------------------------------------------------------------------------

MAX_QUERY = 180

_STOPWORDS = {
    "the", "and", "but", "not", "for", "with", "about", "just", "only",
    "really", "kind", "sort", "like", "mainly", "mostly", "stuff", "things",
    "news", "please", "yeah", "yes", "nah", "some", "any", "that", "this",
    "more", "less", "want", "would", "prefer", "rather", "them", "they",
}


@dataclass
class TopicPlan:
    label: str
    query: str | None = None
    question: str | None = None


def quote(phrase: str) -> str:
    """Multi-word phrases only match as a unit when they are quoted."""
    phrase = re.sub(r'["()]', " ", phrase).strip()
    phrase = re.sub(r"\s+", " ", phrase)
    return f'"{phrase}"' if " " in phrase else phrase


def plain_query(label: str, answer: str | None = None) -> str:
    """The query to use when the model can't write one."""
    query = quote(label)
    # "markets and rates, not crypto" - everything after the negation would
    # otherwise be pulled in as a thing they wanted.
    wanted = re.split(r"\b(?:not|except|excluding|no)\b", answer or "", 1,
                      flags=re.I)[0]
    extras = [
        w for w in re.findall(r"[A-Za-z0-9']{4,}", wanted)
        if w.lower() not in _STOPWORDS and w.lower() not in label.lower()
    ][:4]
    if extras:
        query = f"{query} AND ({' OR '.join(extras)})"
    return query[:MAX_QUERY].strip()


def query_is_sane(query: str) -> bool:
    """Unbalanced quotes or brackets make the news APIs error out."""
    return (
        bool(query)
        and len(query) <= MAX_QUERY
        and query.count('"') % 2 == 0
        and query.count("(") == query.count(")")
    )


MAX_TOPIC_QUESTIONS = 4


async def plan_topic(
    ai: AIBackend | None, label: str, *, transcript: list[dict] | None = None,
) -> TopicPlan:
    """Narrow an interest into a search query, asking until it is pinned down.

    `transcript` is the exchange so far as [{"q": ..., "a": ...}]. The model
    may ask again while there is room, but is told to stop as soon as it could
    write a good query - and is forced to stop after MAX_TOPIC_QUESTIONS, so
    adding a topic can never become an interrogation.
    """
    transcript = list(transcript or [])
    may_ask = len(transcript) < MAX_TOPIC_QUESTIONS
    answers = " ".join(str(t.get("a", "")) for t in transcript).strip()

    if ai is not None:
        parts = [f"Interest: {label}"]
        for turn in transcript:
            parts.append(f"Q: {turn.get('q', '')}")
            parts.append(f"A: {turn.get('a', '')}")
        if not may_ask:
            parts.append("Write the query now. Do not ask anything further.")
        try:
            raw = await ai.complete(
                TOPIC_SYSTEM, "\n".join(parts), max_tokens=350
            )
            data = extract_json(raw)
            if isinstance(data, dict):
                plan = _plan_from_dict(data, label, may_ask=may_ask)
                if plan is not None:
                    return plan
            log.warning("Unparseable topic plan: %s", raw[:200])
        except AIError as exc:
            log.warning("Topic planning unavailable (%s); using a plain query",
                        exc)

    return TopicPlan(label=label, query=plain_query(label, answers or None))


async def refine_query(
    ai: AIBackend | None, label: str, current: str, instruction: str,
) -> tuple[str, str] | None:
    """Adjust a saved query. Returns (new query, short note) or None."""
    if ai is None or not instruction.strip():
        return None
    prompt = (f"Topic: {label}\nCurrent query: {current}\n"
              f"Instruction: {instruction.strip()}")
    try:
        raw = await ai.complete(REFINE_SYSTEM, prompt, max_tokens=250)
    except AIError as exc:
        log.warning("Query refinement unavailable (%s); leaving it alone", exc)
        return None

    data = extract_json(raw)
    if not isinstance(data, dict):
        log.warning("Unparseable refinement: %s", raw[:200])
        return None
    query = str(data.get("query") or "").strip()
    query = re.sub(r"\s+", " ", query)
    if not query_is_sane(query) or query == current:
        return None
    note = _clean(data.get("note")) or "tightened"
    return query, note[:60]


def _plan_from_dict(data: dict, label: str, *, may_ask: bool) -> TopicPlan | None:
    chosen = _clean(data.get("label")) or label
    question = _clean(data.get("question"))
    # NOT _clean: it strips surrounding quotes, and a query that is one
    # quoted phrase would quietly become a loose bag of words.
    query = str(data.get("query") or "").strip()
    if query.lower() in ("", "null", "none"):
        query = None

    if question and may_ask and not query:
        # Keep the label the user's own words until they have answered.
        return TopicPlan(label=label, question=question[:300])
    if not query:
        return None
    query = re.sub(r"\s+", " ", query).strip()
    if not query_is_sane(query):
        log.warning("Discarding malformed query %r", query[:120])
        return TopicPlan(label=chosen, query=plain_query(label))
    return TopicPlan(label=chosen, query=query)


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
# The news chat
# --------------------------------------------------------------------------

_GOODBYE_WORDS = re.compile(
    r"\b(?:thanks?|thank you|ta|cheers|thx|bye|goodbye|good ?night|nite|"
    r"never ?mind|nvm|no more|nothing else|that'?s (?:all|it|enough)|"
    r"i'?m done|done|stop|that'?ll do|that will do)\b",
    re.I,
)


@dataclass
class ChatTurn:
    search: str | None = None
    reply: str | None = None
    end: bool = False
    steer: dict | None = None
    interests: list[dict] = field(default_factory=list)


def is_goodbye(text: str) -> bool:
    """Closing words, recognised without a model call.

    Deliberately narrow: only a short, non-questioning message counts, so
    "thanks to whom?" and "is that deal done?" carry on the conversation
    while "nah I'm done" ends it.
    """
    stripped = text.strip()
    if not stripped or stripped.endswith("?"):
        return False
    words = re.findall(r"[a-z']+", stripped.lower())
    if not words or len(words) > 6:
        return False
    return bool(_GOODBYE_WORDS.search(stripped))


def _history_text(history: list[dict]) -> str:
    lines = [
        f"{'Them' if h.get('role') == 'user' else 'You'}: {h.get('text', '')}"
        for h in (history or [])
    ]
    return "\n".join(lines)


async def plan_chat_turn(
    ai: AIBackend | None, history: list[dict], message: str, *,
    topics: list[str] | None = None,
) -> ChatTurn:
    """Decide whether this message needs a search, an answer, or an ending.

    Also picks up what the conversation says about the topics they follow, so
    a query can be tightened from an ordinary chat rather than a form.
    """
    if is_goodbye(message):
        return ChatTurn(end=True)
    if ai is None:
        # No model: treat whatever they said as the search terms.
        return ChatTurn(search=message.strip())

    prompt = message.strip()
    if past := _history_text(history):
        prompt = f"So far:\n{past}\n\nThem: {message.strip()}"
    prompt = (f"Their topics: {', '.join(topics) if topics else 'none'}\n\n"
              + prompt)
    try:
        raw = await ai.complete(CHAT_SYSTEM, prompt, max_tokens=250)
    except AIError as exc:
        log.warning("Chat planning unavailable (%s); searching instead", exc)
        return ChatTurn(search=message.strip())

    data = extract_json(raw)
    if not isinstance(data, dict):
        log.warning("Unparseable chat turn: %s", raw[:200])
        return ChatTurn(search=message.strip())
    search = _clean(data.get("search"))
    reply = _clean(data.get("reply"))
    end = bool(data.get("end"))
    steer = data.get("steer") if isinstance(data.get("steer"), dict) else None
    interests = [
        {"label": _clean(i.get("label")) or "", "phrase": _clean(i.get("phrase")) or ""}
        for i in (data.get("interests") or [])
        if isinstance(i, dict)
    ]
    interests = [i for i in interests if i["label"] and i["phrase"]]
    if not (search or reply or end):
        return ChatTurn(search=message.strip(), steer=steer, interests=interests)
    return ChatTurn(search=search, reply=reply, end=end, steer=steer,
                    interests=interests)


_CITATION_RE = re.compile(r"\s*\[(\d{1,2})\]")


def link_citations(text: str, articles: list, *, escape=None) -> str:
    """Turn the model's [1] markers into real links.

    The model never writes a URL - it writes a number, and the number is
    looked up here. That way a citation can be wrong about which article it
    points at, but it can never point somewhere that does not exist.
    """
    escape = escape or (lambda t: t)
    body = escape(text)

    def swap(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        if not 0 <= index < len(articles):
            return ""
        article = articles[index]
        source = (getattr(article, "source", "") or "").strip() or "source"
        return f' <a href="{escape(article.url)}">{escape(source)}</a>'

    return _CITATION_RE.sub(swap, body).strip()


async def write_chat_answer(
    ai: AIBackend | None, history: list[dict], message: str, articles: list,
) -> str | None:
    """Prose answering the question, with [n] markers still in place."""
    if ai is None or not articles:
        return None
    numbered = []
    for i, a in enumerate(articles, 1):
        blurb = re.sub(r"\s+", " ", (getattr(a, "description", "") or ""))[:400]
        numbered.append(
            f"{i}. {a.title}\n   source: {a.source}\n   blurb: {blurb}"
        )
    prompt = message.strip()
    if past := _history_text(history):
        prompt = f"So far:\n{past}\n\nThem: {message.strip()}"
    prompt += "\n\nArticles:\n" + "\n".join(numbered)

    try:
        return (await ai.complete(
            CHAT_ANSWER_SYSTEM, prompt, max_tokens=400
        )).strip()
    except AIError as exc:
        log.warning("Chat answer unavailable (%s); listing the articles", exc)
        return None


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
