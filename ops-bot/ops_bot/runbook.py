"""
Runbook fetcher and section matcher for /runbook <topic>.

Fetches RUNBOOK.md from GitHub raw URL at startup and caches for 1h.
Using GitHub raw fetch rather than bundling in the Docker image because
the build context (ops-bot/) can't reach ../docs/ without re-rooting the
Railway build context — fetch-at-startup is simpler and keeps the Dockerfile
unchanged.

Public repo — no auth needed.
"""
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

RUNBOOK_URL = (
    "https://raw.githubusercontent.com/"
    "iancbradley83-arch/pulse-poc/main/docs/RUNBOOK.md"
)
CACHE_TTL = 3600  # 1 hour

_cached_text: Optional[str] = None
_cached_at: float = 0.0


async def _fetch_runbook() -> str:
    global _cached_text, _cached_at
    now = time.monotonic()
    if _cached_text is not None and (now - _cached_at) < CACHE_TTL:
        return _cached_text

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(RUNBOOK_URL)
            resp.raise_for_status()
            text = resp.text
            _cached_text = text
            _cached_at = now
            logger.info("runbook: fetched %d chars from GitHub", len(text))
            return text
    except Exception as exc:
        logger.warning("runbook: fetch failed: %s", exc)
        if _cached_text is not None:
            logger.info("runbook: returning stale cache")
            return _cached_text
        raise RuntimeError(f"runbook unavailable: {exc}") from exc


def _parse_sections(text: str) -> List[Tuple[str, str]]:
    """
    Split markdown on ## headings.
    Returns list of (heading_text, section_body) tuples.
    """
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


async def lookup(topic: str) -> str:
    """
    Return the section(s) matching *topic* (case-insensitive substring).

    - 1 match  → return heading + body
    - N matches → list the heading names
    - 0 matches → list all available headings
    - fetch error → error message
    """
    try:
        text = await _fetch_runbook()
    except RuntimeError as exc:
        return str(exc)

    sections = _parse_sections(text)
    needle = topic.strip().lower()

    # First: match against ## heading text.
    heading_hits = [(h, b) for h, b in sections if needle in h.lower()]

    if len(heading_hits) == 1:
        heading, body = heading_hits[0]
        return f"## {heading}\n\n{body}"
    elif len(heading_hits) > 1:
        names = "\n".join(f"  {h}" for h, _ in heading_hits)
        return f"multiple matches for '{topic}':\n\n{names}\n\nbe more specific."

    # Fallback: match against section body (e.g. /runbook 502 finds the
    # "Incident playbook" section whose body contains "502").
    body_hits = [(h, b) for h, b in sections if needle in b.lower()]

    if len(body_hits) == 1:
        heading, body = body_hits[0]
        return f"## {heading}\n\n{body}"
    elif len(body_hits) > 1:
        names = "\n".join(f"  {h}" for h, _ in body_hits)
        return f"multiple matches for '{topic}':\n\n{names}\n\nbe more specific."

    available = "\n".join(f"  {h}" for h, _ in sections)
    return f"no section matching '{topic}'. available topics:\n\n{available}"
