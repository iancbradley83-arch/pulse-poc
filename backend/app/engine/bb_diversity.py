"""Phase 3b: Bet Builder + combo diversity classification.

Pure-function module that classifies cards along two axes — leg count
and odds bucket — and computes the actual composition of a generated
candidate pool against design targets. Lets us answer:

  * Are we generating BBs across the full 2-6 leg range, or are they
    all clustered at 2-3 legs because that's what the themes default to?
  * Are we mostly producing low-odds singles, or do we have enough
    longshot variety for users to find a card that excites them?
  * Did the new market-pool breadth (Phase 3b PR #119) actually produce
    proportionally more BB variety, or did the candidate engine's
    upstream filters squeeze the diversity back out?

Per `feedback_calibrate_before_scoring`: ship the composition reporter
as observability first. Once we have 1-2 cycles of "what we actually
produce", THEN tune the diversity targets and force the engine to hit
them. Don't pre-calibrate.

## Targets

`target_leg_distribution` and `target_odds_distribution` return the
DESIRED mix per importance score — the reporter logs these alongside
the actual composition so we can see drift. They're hand-tuned starting
points; the eventual route layer (PR #120) will use the gap between
target and actual to pick which candidate themes to favour.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.models.news import BetType, CandidateCard


# ── Odds bucketing ───────────────────────────────────────────────────────

# Boundaries chosen so a "typical" Pulse single (1X2 Premier League
# favourite, 1.50–2.20) lands in `mid`, longshot goalscorer 6.0+ lands
# in `longshot`, and an outright 25+ lands in `lottery`. Tuned for
# narrative variety, not statistical edge — we'll calibrate from real
# distributions in PR #120.
ODDS_BUCKET_BOUNDARIES = [
    ("short", 1.0, 1.50),     # heavy favourites, short anchor legs
    ("mid", 1.50, 2.50),      # typical singles + 2-leg BBs
    ("plus", 2.50, 5.00),     # value singles, 3-4 leg BBs
    ("long", 5.00, 12.00),    # longshot scorers, 5+ leg BBs
    ("lottery", 12.00, float("inf")),
]


def bucket_for_odds(odds: Optional[float]) -> str:
    """Return the bucket name for a price. `None` and 0/negative → "unknown"."""
    if odds is None or not isinstance(odds, (int, float)) or odds <= 1.0:
        return "unknown"
    for name, lo, hi in ODDS_BUCKET_BOUNDARIES:
        if lo <= odds < hi:
            return name
    return "unknown"


# ── Targets per importance score ─────────────────────────────────────────


def target_leg_distribution(importance_score: Optional[float]) -> dict[int, float]:
    """Desired share of cards at each leg count, per importance.

    Top-importance fixtures get a wider spread (2..6 legs); tail
    fixtures concentrate at 2-3 legs to keep cost down. Values are
    fractions summing to 1.0; absolute counts depend on the per-fixture
    candidate cap from gradient routing.
    """
    s = 1.0 if importance_score is None else max(0.0, min(1.0, float(importance_score)))
    # Linear blend between tail (s=0) and top (s=1) targets.
    tail = {1: 0.50, 2: 0.30, 3: 0.20, 4: 0.0, 5: 0.0, 6: 0.0}
    top = {1: 0.20, 2: 0.20, 3: 0.25, 4: 0.20, 5: 0.10, 6: 0.05}
    return {k: round(tail[k] + s * (top[k] - tail[k]), 3) for k in tail}


def target_odds_distribution(importance_score: Optional[float]) -> dict[str, float]:
    """Desired share of cards in each odds bucket, per importance."""
    s = 1.0 if importance_score is None else max(0.0, min(1.0, float(importance_score)))
    tail = {"short": 0.30, "mid": 0.50, "plus": 0.15, "long": 0.05, "lottery": 0.0}
    top = {"short": 0.15, "mid": 0.35, "plus": 0.30, "long": 0.15, "lottery": 0.05}
    return {k: round(tail[k] + s * (top[k] - tail[k]), 3) for k in tail}


# ── Card classification ──────────────────────────────────────────────────


def leg_count_for_card(card: CandidateCard) -> int:
    """Number of legs for a card.

    Singles always count as 1 (a single-market card has one selection).
    Combos/BBs count their `selection_ids`. A misshapen card with 0
    selections falls back to 1 to keep counts non-zero.
    """
    if card.bet_type == BetType.SINGLE:
        return 1
    n = len(card.selection_ids or [])
    return n if n > 0 else 1


def composition_report(cards: Iterable[CandidateCard]) -> dict[str, Any]:
    """Classify a list of cards by (bet_type, leg_count, odds_bucket).

    Returns a JSON-shaped dict suitable for logging or the admin
    endpoint. Every counter is non-negative and bounded by `len(cards)`.
    """
    cards_list = list(cards)
    total = len(cards_list)
    by_bet_type: dict[str, int] = {}
    by_leg_count: dict[int, int] = {}
    by_odds_bucket: dict[str, int] = {}
    by_hook: dict[str, int] = {}
    bb_legs_only: dict[int, int] = {}
    odds_min: Optional[float] = None
    odds_max: Optional[float] = None
    odds_present_count = 0

    for c in cards_list:
        bt = (c.bet_type.value if hasattr(c.bet_type, "value") else str(c.bet_type))
        by_bet_type[bt] = by_bet_type.get(bt, 0) + 1
        legs = leg_count_for_card(c)
        by_leg_count[legs] = by_leg_count.get(legs, 0) + 1
        if c.bet_type != BetType.SINGLE:
            bb_legs_only[legs] = bb_legs_only.get(legs, 0) + 1
        bucket = bucket_for_odds(c.total_odds)
        by_odds_bucket[bucket] = by_odds_bucket.get(bucket, 0) + 1
        if isinstance(c.total_odds, (int, float)) and c.total_odds > 1.0:
            odds_present_count += 1
            if odds_min is None or c.total_odds < odds_min:
                odds_min = c.total_odds
            if odds_max is None or c.total_odds > odds_max:
                odds_max = c.total_odds
        hook = (c.hook_type.value if hasattr(c.hook_type, "value") else str(c.hook_type))
        by_hook[hook] = by_hook.get(hook, 0) + 1

    return {
        "total_cards": total,
        "by_bet_type": dict(sorted(by_bet_type.items(), key=lambda kv: -kv[1])),
        "by_leg_count": dict(sorted(by_leg_count.items())),
        "bb_or_combo_by_leg_count": dict(sorted(bb_legs_only.items())),
        "by_odds_bucket": {
            name: by_odds_bucket.get(name, 0)
            for name, _, _ in ODDS_BUCKET_BOUNDARIES
        } | (
            {"unknown": by_odds_bucket["unknown"]}
            if "unknown" in by_odds_bucket else {}
        ),
        "by_hook": dict(sorted(by_hook.items(), key=lambda kv: -kv[1])),
        "odds_range": {
            "min": odds_min, "max": odds_max,
            "with_odds": odds_present_count,
        },
    }


def format_composition_log_line(report: dict[str, Any]) -> str:
    """Single-line summary for end-of-cycle log emission."""
    bt = report["by_bet_type"]
    legs = report["by_leg_count"]
    odds = report["by_odds_bucket"]
    rng = report["odds_range"]
    legs_str = ", ".join(f"{k}={v}" for k, v in legs.items())
    odds_str = ", ".join(f"{k}={v}" for k, v in odds.items() if v)
    bt_str = ", ".join(f"{k}={v}" for k, v in bt.items())
    rng_str = (
        f"odds_range=[{rng['min']:.2f}, {rng['max']:.2f}]"
        if rng["min"] is not None and rng["max"] is not None
        else "odds_range=n/a"
    )
    return (
        f"total={report['total_cards']}  bet_type={{{bt_str}}}  "
        f"legs={{{legs_str}}}  odds_bucket={{{odds_str}}}  {rng_str}"
    )
