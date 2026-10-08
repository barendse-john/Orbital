"""All the AI prompting lives here: turning an interest into a search query,
and summarising articles for the briefing.

Every function degrades gracefully - if the model is unreachable or answers
with nonsense, a plain keyword query or the article's own blurb is used and
everything keeps working.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .ai import AIBackend, AIError

log = logging.getLogger(__name__)

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

SUMMARY_SYSTEM = """You write one-line news summaries for a news briefing.
For each numbered article you receive, write ONE sentence of at most 20 words
saying what actually happened - concrete facts, no hype, no "this article
discusses". Reply with a JSON array of strings, one per article, in the same
order. No other text."""


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


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().strip('"').strip()
    return text or None


# --------------------------------------------------------------------------
# Topics: one clarifying question, then a query worth searching
# --------------------------------------------------------------------------

MAX_QUERY = 180

# Words that never narrow a news search on their own.
_STOPWORDS = {
    "a", "about", "all", "an", "and", "any", "are", "at", "be", "been",
    "best", "but", "for", "how", "i", "in", "into", "is", "it", "its", "just",
    "kind", "latest", "less", "like", "mainly", "me", "more", "most",
    "mostly", "my", "nah", "new", "news", "not", "now", "of", "on", "only",
    "or", "our", "please", "prefer", "rather", "really", "recent", "recently",
    "right", "some", "sort", "stuff", "that", "the", "their", "them", "there",
    "these", "they", "things", "this", "those", "to", "top", "us", "want",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "with", "would", "yeah", "yes", "you", "your",
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
                TOPIC_SYSTEM, "\n".join(parts), max_tokens=350, attempts=1
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
# Summaries
# --------------------------------------------------------------------------

async def summarise_articles(
    ai: AIBackend | None, articles: list, *, max_tokens_per_article: int = 60,
    attempts: int | None = None,
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
            attempts=attempts,
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
