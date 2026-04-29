"""
/preview — data-view of the live widget.

Fetches /api/feed and renders a phone-readable summary of what users see right
now: top 5 cards by relevance_score (descending), each with a HEAD-request
check on the deep_link URL.

Public API
----------
build_preview(pulse_client) -> str
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .formatting import _team_name, _truncate_at_word
from .pulse_client import PulseClient, PulseError

logger = logging.getLogger(__name__)

DEEP_LINK_TIMEOUT = 3.0  # seconds per HEAD request
PREVIEW_CARD_COUNT = 5


async def _head_status(url: str) -> Optional[int]:
    """
    Issue a HEAD request to url and return the HTTP status code.

    Returns None on timeout or connection error (non-fatal).
    """
    try:
        async with httpx.AsyncClient(timeout=DEEP_LINK_TIMEOUT, follow_redirects=True) as client:
            resp = await client.head(url)
            return resp.status_code
    except httpx.TimeoutException:
        logger.debug("preview: HEAD %s timed out", url)
        return None
    except Exception as exc:
        logger.debug("preview: HEAD %s error: %s", url, exc)
        return None


def _card_preview_block(card: Dict[str, Any], deep_link_status: Optional[int]) -> str:
    """Render a single card as a 4-line phone-readable block."""
    cid = (card.get("id") or "")[:8] or "????????"
    hook = card.get("hook_type") or card.get("bet_type") or "?"

    relevance = card.get("relevance_score")
    relevance_str = f"{relevance:.2f}" if relevance is not None else "n/a"

    game = card.get("game") or {}
    home = _team_name(game.get("home_team") or game.get("home"))
    away = _team_name(game.get("away_team") or game.get("away"))
    league_obj = game.get("league") or {}
    league = (
        (league_obj.get("name") if isinstance(league_obj, dict) else None)
        or card.get("league")
        or game.get("league_name")
        or ""
    )

    narrative = (card.get("narrative_hook") or card.get("headline") or "").strip()
    narrative = _truncate_at_word(narrative, 100)

    deep_link = card.get("deep_link") or card.get("deeplink") or card.get("url") or ""

    lines = [f"[{cid}] · {hook} · {relevance_str}"]

    game_line = f"{home} vs {away}"
    if league:
        game_line += f" · {league}"
    if home != "?" or away != "?":
        lines.append(game_line)

    if narrative:
        lines.append(f'"{narrative}"')

    if deep_link:
        if deep_link_status is None:
            status_label = "HEAD: timeout"
        elif 200 <= deep_link_status < 300:
            status_label = f"HEAD: {deep_link_status}"
        else:
            status_label = f"HEAD: {deep_link_status} (!)"
        lines.append(f"deep_link: {deep_link}  ({status_label})")
    else:
        lines.append("deep_link: (none)")

    return "\n".join(lines)


async def build_preview(pulse_client: PulseClient) -> str:
    """
    Fetch /api/feed and render a preview of the top 5 cards by relevance_score.

    Returns a formatted string ready to send as a Telegram message.
    """
    try:
        feed_data = await pulse_client.feed()
    except PulseError as exc:
        return f"preview: could not fetch feed — {exc}"

    all_cards: List[Dict[str, Any]] = feed_data.get("cards", [])
    total_cards = len(all_cards)

    if not all_cards:
        return "preview: feed is empty right now"

    # Sort by relevance_score descending; cards without a score go to the end.
    def _relevance_key(c: Dict[str, Any]) -> float:
        r = c.get("relevance_score")
        return float(r) if r is not None else -1.0

    sorted_cards = sorted(all_cards, key=_relevance_key, reverse=True)
    top_cards = sorted_cards[:PREVIEW_CARD_COUNT]

    # Collect deep_link URLs for concurrent HEAD requests.
    deep_links: List[Optional[str]] = [
        card.get("deep_link") or card.get("deeplink") or card.get("url") or None
        for card in top_cards
    ]

    # Run HEAD requests concurrently (3s timeout each, non-fatal).
    # Cards without a deep_link get None without making any network call.
    head_results: List[Optional[int]] = list(
        await asyncio.gather(
            *[_head_status(url) if url else _none_coro() for url in deep_links],
            return_exceptions=False,
        )
    )

    # Find the latest published_at for the footer.
    latest_published: Optional[str] = None
    for card in all_cards:
        pub = card.get("published_at") or ""
        if pub:
            if latest_published is None or pub > latest_published:
                latest_published = pub

    # Build blocks.
    blocks = []
    for card, status in zip(top_cards, head_results):
        blocks.append(_card_preview_block(card, status))

    body = "\n\n".join(blocks)

    footer_parts = [f"total cards in feed: {total_cards}"]
    if latest_published:
        footer_parts.append(f"feed last updated: {latest_published}")
    footer = "  ·  ".join(footer_parts)

    return body + f"\n\n— {footer} —"


async def _none_coro() -> None:
    """Placeholder coroutine that returns None for cards without a deep_link."""
    return None
