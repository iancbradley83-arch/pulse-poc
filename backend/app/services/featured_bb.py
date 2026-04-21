"""Featured Bet Builders — operator-curated, pre-built BBs from Rogue.

Source: GET /v1/featured/betbuilder. Returns ~6 BBs the operator has hand-
picked as recommendations. Each item carries a `VirtualId` (the same
`0VS<piped-leg-ids>` shape we already use with `calculate_bets`) and an
`Info` array with per-leg `MarketName` / `SelectionName` / `EventName`
strings ready for display.

The endpoint does NOT include prices — we still call `calculate_bets`
with the VirtualId to get the correlated `TrueOdds`, just like for our
news-driven BBs.

Why these are surfaced as fully self-contained Card objects (bypassing
candidate_store): they're not from our engine, they don't need quality
gating, and their leg markets (e.g. anytime Goalscorer, Corners O/U) are
typically outside our `MarketCatalog` whitelist — so the publish path's
catalog-resolution step would drop them. Building Cards directly from the
API response keeps featured BBs working without a `MarketCatalog`
expansion that hasn't shipped yet.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.models.schemas import (
    BadgeType,
    Card,
    CardLeg,
    CardType,
    Game,
)
from app.services.rogue_client import RogueClient

logger = logging.getLogger(__name__)


def _build_name_index(games_by_id: dict[str, Game]) -> dict[str, Game]:
    """Build a `lower(EventName) → Game` index for matching featured-BB
    Items by their EventName field. Featured BBs that don't match any
    game in our catalogue are filtered out (out of scope — typically
    Brazilian / non-international fixtures)."""
    out: dict[str, Game] = {}
    for g in games_by_id.values():
        name = f"{g.home_team.name} vs {g.away_team.name}".strip().lower()
        out[name] = g
        # Also index a couple alternate forms for fuzzier matches (some Rogue
        # responses use 'X v Y' or just 'X - Y').
        for sep in (" v ", " - "):
            out[name.replace(" vs ", sep)] = g
    return out


async def fetch_and_build_featured_bb_cards(
    client: RogueClient,
    games_by_id: dict[str, Game],
    *,
    max_count: int = 6,
    locale: str = "en",
) -> list[Card]:
    """Pull operator-curated featured BBs, price each via calculate_bets,
    return as Card objects ready for FeedManager.

    Each card has bet_type='bet_builder', hook_type='featured', and is
    tagged with a 'Recommended' source label so the UI can distinguish
    from news-driven BBs.
    """
    try:
        data = await client.featured_betbuilders(locale=locale)
    except Exception as exc:
        logger.warning("[featured_bb] fetch failed: %s", exc)
        return []
    items = (data or {}).get("Items") or []
    if not items:
        logger.info("[featured_bb] no items returned")
        return []

    name_index = _build_name_index(games_by_id)
    cards: list[Card] = []

    for item in items:
        if len(cards) >= max_count:
            break
        vid: Optional[str] = item.get("VirtualId")
        info: list[dict[str, Any]] = item.get("Info") or []
        if not vid or len(info) < 2:
            continue

        # Filter to events in our catalogue (international leagues only).
        event_name = (info[0].get("EventName") or "").strip()
        game = name_index.get(event_name.lower())
        if game is None:
            logger.info("[featured_bb] skipping %r — not in current catalogue", event_name)
            continue

        # Price it
        try:
            quote = await client.calculate_bets([vid])
        except Exception as exc:
            logger.warning("[featured_bb] calculate_bets errored for %s: %s", vid[:48], exc)
            continue
        if not isinstance(quote, dict):
            continue
        bets = quote.get("Bets") or []
        bb_bet = next(
            (b for b in bets if (b or {}).get("Type") in ("BetBuilder", "Single")),
            None,
        )
        if not bb_bet or not isinstance(bb_bet.get("TrueOdds"), (int, float)):
            logger.warning(
                "[featured_bb] no usable price for %s (bet_types=%s)",
                vid[:48], [b.get("Type") for b in bets],
            )
            continue
        total_odds = round(float(bb_bet["TrueOdds"]), 2)

        # Build legs from Info[] augmented with per-leg odds from the
        # calculate_bets Selections[] array.
        sel_by_id = {s.get("Id"): s for s in (quote.get("Selections") or [])}
        legs: list[CardLeg] = []
        for leg_info in info:
            sid = leg_info.get("SelectionId")
            sel = sel_by_id.get(sid) or {}
            try:
                leg_odds = float(sel.get("TrueOdds") or 0)
            except (TypeError, ValueError):
                leg_odds = 0.0
            legs.append(CardLeg(
                label=leg_info.get("SelectionBetSlipLine")
                      or leg_info.get("SelectionName") or "",
                market_label=leg_info.get("MarketName") or "",
                odds=leg_odds,
                selection_id=sid,
            ))

        card = Card(
            card_type=CardType.PRE_MATCH,
            game=game,
            badge=BadgeType.TRENDING,
            narrative_hook=f"Operator's pick for {event_name}",
            relevance_score=0.85,
            ttl_seconds=3600,
            hook_type="featured",
            headline=f"Recommended Bet Builder · {event_name}",
            source_name="Apuesta Total · Featured",
            legs=legs,
            total_odds=total_odds,
            bet_type="bet_builder",
            virtual_selection=vid,
        )
        cards.append(card)
        logger.info(
            "[featured_bb] added %s @ %.2f (legs=%d)",
            event_name, total_odds, len(legs),
        )

    return cards
