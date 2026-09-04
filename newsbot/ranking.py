"""Deciding which of the day's stories are worth the digest.

Four cheap signals, no model call:

* **corroboration** - how many different outlets ran the story. The strongest
  signal there is: a thing several newsrooms bothered with is a thing that
  happened. Headlines differ between outlets, so stories are grouped on the
  words they share rather than on an exact match.
* **topic match** - how much of the user's own search query the story
  actually contains. A story hitting three phrases beats one hitting a word.
* **source tier** - trusted newsrooms up, press-release wires and horoscope
  filler down. University and research publications are deliberately NOT
  demoted: they break developing-technology stories first.
* **recency** - later in the day wins, so the digest opens on what was still
  moving at bedtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

WEIGHTS = {
    "corroboration": 1.6,
    "topic": 1.2,
    "source": 1.4,
    "recency": 0.6,
}

# Outlets whose presence is itself evidence the story matters.
TRUSTED = re.compile(
    r"reuters|bloomberg|associated press|\bap news\b|financial times|\bft\.com"
    r"|wall street journal|\bwsj\b|the economist|bbc|guardian|new york times"
    r"|\bnyt\b|washington post|al jazeera|npr|politico|axios|nikkei|cnbc"
    r"|nature|science(mag|daily)?\.|scientific american|new scientist|ieee"
    r"|ars technica|spacenews|aviation week|the verge|techcrunch|wired"
    r"|defense news|janes|space\.com|phys\.org",
    re.I,
)

# Research and teaching institutions. Neutral by choice - their press offices
# are often where a new technology is written up first.
ACADEMIC = re.compile(
    r"universit|\.edu\b|institute|laborator|\bmit\b|caltech|\beth\b|tu delft"
    r"|max planck|fraunhofer|cnrs|academy of sciences|college of engineering",
    re.I,
)

# Wire dumps, SEO filler and syndication farms.
LOW_QUALITY = re.compile(
    r"ein ?news|einpresswire|pr ?newswire|business ?wire|globe ?newswire"
    r"|accesswire|openpr|prweb|press ?release|horoscope|financescope"
    r"|astrology|zodiac|sponsored|advertorial|marketscreener|simply wall st"
    r"|\bbest .* deals\b|coupon",
    re.I,
)

SOURCE_SCORES = {"trusted": 1.0, "academic": 0.65, "neutral": 0.5, "low": 0.05}

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "new", "says", "say",
    "said", "after", "over", "into", "amid", "will", "has", "have", "had",
    "how", "why", "what", "who", "more", "than", "about", "up", "down", "out",
    "not", "no", "you", "your", "we", "our", "they", "their", "he", "she",
    "his", "her", "can", "could", "may", "might", "first", "news", "report",
    "reports", "year", "years", "day", "days", "week", "top", "best",
}


def keywords(text: str) -> set[str]:
    """The words that carry meaning, for comparing two headlines."""
    return {
        w for w in _WORD.findall((text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def source_tier(article) -> str:
    haystack = f"{getattr(article, 'source', '')} {getattr(article, 'url', '')}"
    if LOW_QUALITY.search(haystack):
        return "low"
    if TRUSTED.search(haystack):
        return "trusted"
    if ACADEMIC.search(haystack):
        return "academic"
    return "neutral"


def same_story(a_words: set[str], b_words: set[str]) -> bool:
    """Do two headlines describe one event?

    Outlets rewrite headlines, so an exact match finds almost nothing. Overlap
    against the shorter headline catches "Nvidia buys Hugging Face for $13bn"
    and "Nvidia confirms $13 billion Hugging Face acquisition" without pulling
    in every other Nvidia story.
    """
    if not a_words or not b_words:
        return False
    shared = a_words & b_words
    return len(shared) >= 3 and len(shared) / min(len(a_words), len(b_words)) >= 0.5


@dataclass
class Cluster:
    """One event, as told by one or more outlets."""

    lead: object
    members: list = field(default_factory=list)
    words: set[str] = field(default_factory=set)

    @property
    def sources(self) -> set[str]:
        return {
            (getattr(m, "source", "") or getattr(m, "url", "")).lower()
            for m in self.members
        }


def cluster_articles(articles: list) -> list[Cluster]:
    """Group the pool into events, keeping the best-sourced telling of each."""
    clusters: list[Cluster] = []
    for article in articles:
        words = keywords(article.title)
        for cluster in clusters:
            if same_story(words, cluster.words):
                cluster.members.append(article)
                cluster.words |= words
                # The strongest outlet gets to be the one we link to.
                if _tier_rank(article) > _tier_rank(cluster.lead):
                    cluster.lead = article
                break
        else:
            clusters.append(Cluster(lead=article, members=[article], words=words))
    return clusters


def _tier_rank(article) -> float:
    return SOURCE_SCORES[source_tier(article)]


def topic_match(article, query: str) -> float:
    """How much of the search query the story actually contains, 0-1."""
    phrases = re.findall(r'"([^"]+)"', query or "")
    loose = keywords(re.sub(r'"[^"]*"', " ", query or ""))
    haystack = f"{article.title} {getattr(article, 'description', '')}".lower()
    terms = [p.lower() for p in phrases] + sorted(loose)
    if not terms:
        return 0.5
    hits = sum(1 for t in terms if t in haystack)
    return min(1.0, hits / min(len(terms), 3))


def recency(article, now: datetime | None = None) -> float:
    published = getattr(article, "published_at", None)
    if published is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    hours = (now - published).total_seconds() / 3600
    return max(0.0, min(1.0, 1 - hours / 24))


@dataclass
class Scored:
    article: object
    score: float
    sources: int
    parts: dict


def score_cluster(cluster: Cluster, query: str,
                  now: datetime | None = None) -> Scored:
    outlets = len(cluster.sources)
    parts = {
        "corroboration": min(outlets, 4) / 4,
        "topic": topic_match(cluster.lead, query),
        "source": SOURCE_SCORES[source_tier(cluster.lead)],
        "recency": recency(cluster.lead, now),
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return Scored(cluster.lead, round(total, 3), outlets, parts)


def rank(articles_by_topic: list[tuple[str, str, list]],
         now: datetime | None = None) -> list[tuple[str, Scored]]:
    """[(label, query, articles)] -> [(label, Scored)], best first.

    Clustering runs across every topic at once, so a story two topics both
    turned up counts as corroborated rather than appearing twice.
    """
    owner: dict[int, str] = {}
    pool: list = []
    for label, _query, articles in articles_by_topic:
        for article in articles:
            owner.setdefault(id(article), label)
            pool.append(article)

    queries = {label: query for label, query, _ in articles_by_topic}
    scored: list[tuple[str, Scored]] = []
    for cluster in cluster_articles(pool):
        label = owner[id(cluster.lead)]
        scored.append((label, score_cluster(cluster, queries.get(label, ""), now)))
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    return scored


def take_top(ranked: list[tuple[str, Scored]], total: int,
             per_topic: int = 3) -> list[tuple[str, Scored]]:
    """The best `total` stories, with no single topic swallowing the digest."""
    counts: dict[str, int] = {}
    kept: list[tuple[str, Scored]] = []
    for label, scored in ranked:
        if len(kept) >= total:
            break
        if counts.get(label, 0) >= per_topic:
            continue
        counts[label] = counts.get(label, 0) + 1
        kept.append((label, scored))
    return kept
