"""
Feed audit helpers for /feed and /feed page <n>.

Inlines logic from the pulse-feed-audit skill; does not import it at runtime.
"""
from collections import Counter
from typing import Any, Dict, List, Tuple

PAGE_SIZE = 5


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _has_missing_price(card: Dict[str, Any]) -> bool:
    """Return True if total_odds is null or any leg price is null."""
    if card.get("total_odds") is None:
        return True
    for leg in card.get("legs", []):
        if leg.get("price") is None:
            return True
    return False


def build_feed_summary(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return a summary dict from a list of card dicts.

    Keys:
      total          int
      by_hook_type   list[(hook_type, count)]  sorted desc
      by_league      list[(league_name, count)] top 5 desc
      missing_price  int
      suspended      int
      avg_relevance  float | None
    """
    total = len(cards)

    hook_counter: Counter = Counter()
    league_counter: Counter = Counter()
    missing_price = 0
    suspended = 0
    relevance_scores: List[float] = []

    for card in cards:
        hook_counter[card.get("hook_type") or "unknown"] += 1

        # League name: try nested game.league.name, fallback to direct league field.
        game = card.get("game") or {}
        league_obj = game.get("league") or {}
        league_name = (
            league_obj.get("name")
            or card.get("league")
            or game.get("league_name")
            or "unknown"
        )
        league_counter[league_name] += 1

        if _has_missing_price(card):
            missing_price += 1

        if card.get("suspended", False):
            suspended += 1

        rs = card.get("relevance_score")
        if rs is not None:
            try:
                relevance_scores.append(float(rs))
            except (TypeError, ValueError):
                pass

    avg_relevance = (
        round(sum(relevance_scores) / len(relevance_scores), 2)
        if relevance_scores
        else None
    )

    return {
        "total": total,
        "by_hook_type": hook_counter.most_common(),
        "by_league": league_counter.most_common(5),
        "missing_price": missing_price,
        "suspended": suspended,
        "avg_relevance": avg_relevance,
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def get_page(cards: List[Dict[str, Any]], page: int) -> Tuple[List[Dict[str, Any]], int]:
    """
    Return (page_cards, total_pages).

    page is 1-indexed. If out of range, page_cards is empty and total_pages
    is the actual total (caller renders "no such page").
    """
    total_pages = max(1, (len(cards) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page < 1 or page > total_pages:
        return [], total_pages
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    return cards[start:end], total_pages


def _card_row(card: Dict[str, Any]) -> str:
    """Format a single card for the paginated list."""
    card_id = (card.get("id") or "")[:8]
    hook_type = card.get("hook_type") or "unknown"

    game = card.get("game") or {}
    league_obj = game.get("league") or {}
    league = (
        league_obj.get("name")
        or card.get("league")
        or game.get("league_name")
        or "—"
    )

    narrative = (card.get("narrative_hook") or card.get("headline") or "").strip()
    if len(narrative) > 60:
        narrative = narrative[:57] + "..."

    odds = card.get("total_odds")
    odds_str = f"${odds:.2f}" if odds is not None else "n/a"

    return f"{card_id}  {hook_type}  {league}  {narrative}  {odds_str}"
