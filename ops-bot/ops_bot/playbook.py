"""
Playbook fetcher and section matcher for /playbook <topic>.

Mirrors the runbook.py pattern: fetch PLAYBOOK.md from GitHub raw on first
call, cache 1h. Public repo so no auth needed.

PLAYBOOK.md sections are headed with `## Scenario: <name>` (operational
scenarios) plus a few non-scenario sections like `## Coverage matrix`,
`## Learning loop`, `## When to wake up vs sleep through`. Match topic
against heading text first (case-insensitive substring), fall back to
body match (e.g. /playbook cost finds the cost ladder scenario via
"cost ladder" in body).
"""
import logging
import re
import time
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

PLAYBOOK_URL = (
    "https://raw.githubusercontent.com/"
    "iancbradley83-arch/pulse-poc/main/docs/PLAYBOOK.md"
)
CACHE_TTL = 3600  # 1 hour

_cached_text: Optional[str] = None
_cached_at: float = 0.0


async def _fetch_playbook() -> str:
    global _cached_text, _cached_at
    now = time.monotonic()
    if _cached_text is not None and (now - _cached_at) < CACHE_TTL:
        return _cached_text

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(PLAYBOOK_URL)
            resp.raise_for_status()
            text = resp.text
            _cached_text = text
            _cached_at = now
            logger.info("playbook: fetched %d chars from GitHub", len(text))
            return text
    except Exception as exc:
        logger.warning("playbook: fetch failed: %s", exc)
        if _cached_text is not None:
            logger.info("playbook: returning stale cache")
            return _cached_text
        raise RuntimeError(f"playbook unavailable: {exc}") from exc


def _parse_sections(text: str) -> List[Tuple[str, str]]:
    """Split markdown on ## headings. Returns list of (heading, body)."""
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    sections: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((heading, body))
    return sections


# NB: order matters. The first hint to match the heading wins.
# "feed" must come before "health" because "feed unhealthy" contains
# both "feed" and "health" (in "unhealthy"). Similarly "deep-link"
# before "deeplink" so the hyphenated form is preferred when present.
_HEADING_TO_SLUG_HINTS = (
    ("coverage", "coverage"),
    ("matrix", "coverage"),
    ("cost", "cost"),
    ("deploy", "deploy"),
    ("feed", "feed"),
    ("deep-link", "deeplink"),
    ("deeplink", "deeplink"),
    ("paused", "paused"),
    ("bad card", "badcard"),
    ("data loss", "data"),
    ("sqlite", "data"),
    ("bot itself", "bot"),
    ("anthropic", "api"),
    ("rogue", "api"),
    ("operator", "operator"),
    # Note: "frontend / widget broken" must come before "health" because
    # the heading reads "Scenario: frontend / widget broken (200 OK, page
    # won't render)" — and "OK" doesn't trigger but "health" elsewhere could.
    ("frontend", "frontend"),
    ("widget broken", "frontend"),
    ("health", "health"),
    ("learning", "learning"),
    ("loop", "learning"),
    ("wake up", "wake"),
    ("sleep", "wake"),
    ("adding", "adding"),
)


def slug_for(heading: str) -> str:
    """
    Derive a short tappable slug for a section heading.

    Slugs must match `[a-z]+` so they can be appended to '/playbook_' and
    rendered as a single tappable command in Telegram.
    """
    h = heading.lower()
    for keyword, slug in _HEADING_TO_SLUG_HINTS:
        if keyword in h:
            return slug
    # Fallback: first alphabetic word from the heading.
    words = re.findall(r"[a-z]+", h.replace("scenario:", ""))
    return words[0] if words else "topic"


async def list_topics() -> Optional[List[str]]:
    """Return the list of section headings, or None if fetch fails."""
    try:
        text = await _fetch_playbook()
    except RuntimeError:
        return None
    return [h for h, _ in _parse_sections(text)]


async def list_topics_with_slugs() -> Optional[List[tuple]]:
    """Return [(heading, slug)] pairs, or None if fetch fails."""
    try:
        text = await _fetch_playbook()
    except RuntimeError:
        return None
    return [(h, slug_for(h)) for h, _ in _parse_sections(text)]


async def lookup(topic: str) -> str:
    """
    Return the section(s) matching *topic* (case-insensitive substring).

    - 1 match  -> heading + body
    - N matches -> list the heading names
    - 0 matches -> list available
    - fetch error -> error string
    """
    try:
        text = await _fetch_playbook()
    except RuntimeError as exc:
        return str(exc)

    sections = _parse_sections(text)
    needle = topic.strip().lower()

    # First: match on heading text.
    heading_hits = [(h, b) for h, b in sections if needle in h.lower()]

    if len(heading_hits) == 1:
        heading, body = heading_hits[0]
        return f"## {heading}\n\n{body}"
    elif len(heading_hits) > 1:
        names = "\n".join(f"  {h}" for h, _ in heading_hits)
        return f"multiple matches for '{topic}':\n\n{names}\n\nbe more specific."

    # Fallback: match body content.
    body_hits = [(h, b) for h, b in sections if needle in b.lower()]

    if len(body_hits) == 1:
        heading, body = body_hits[0]
        return f"## {heading}\n\n{body}"
    elif len(body_hits) > 1:
        names = "\n".join(f"  {h}" for h, _ in body_hits)
        return f"multiple matches for '{topic}':\n\n{names}\n\nbe more specific."

    available = "\n".join(f"  {h}" for h, _ in sections)
    return f"no section matching '{topic}'. available topics:\n\n{available}"
