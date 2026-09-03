"""Turning articles into Telegram messages (HTML parse mode)."""

from __future__ import annotations

from datetime import datetime
from html import escape

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3800  # leaves room for the header on continuation messages


def esc(text: str) -> str:
    return escape(text or "", quote=False)


def article_line(article, *, bullet: str = "•") -> str:
    line = f'{bullet} <a href="{esc(article.url)}">{esc(article.title)}</a>'
    summary = (article.summary or "").strip()
    if summary:
        line += f"\n   <i>{esc(summary)}</i>"
    if article.source:
        line += f"\n   <i>{esc(article.source)}</i>" if not summary \
            else f" <i>— {esc(article.source)}</i>"
    return line


def topic_block(label: str, articles: list) -> str:
    lines = [f"<b>{esc(label)}</b>"]
    lines.extend(article_line(a) for a in articles)
    return "\n".join(lines)


def digest_messages(
    blocks: list[str], *, when: datetime | None = None,
    empty_topics: list[str] | None = None,
) -> list[str]:
    """One combined message, split only if Telegram's limit forces it."""
    when = when or datetime.now()
    header = f"🗞 <b>Your news digest</b> · {when.strftime('%a %-d %b, %H:%M')}"

    footer = ""
    if empty_topics:
        names = ", ".join(esc(t) for t in empty_topics)
        footer = f"<i>Nothing new on: {names}</i>"

    parts = [header, *blocks]
    if footer:
        parts.append(footer)

    return chunk("\n\n".join(parts))


def chunk(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split on blank lines first, then newlines, so links never break."""
    if len(text) <= limit:
        return [text]

    messages: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        if len(block) <= limit:
            current = block
            continue
        # A single oversized block: split it line by line.
        current = ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    messages.append(current)
                current = line[:limit]
    if current:
        messages.append(current)
    return messages
