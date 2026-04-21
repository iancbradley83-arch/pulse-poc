"""Quality gates — fail-closed rules that drop bad candidates before publish.

Every rule here should map to a specific failure mode we've seen in the admin
table or on the live feed. When in doubt, reject. The admin table lists
rejected candidates too (with their reason) so we can audit false positives.

Order of enforcement (first failure wins):

  1. Headline sanity       — length, tics, substance
  2. Angle sanity          — length, forbidden phrases
  3. BB sanity             — leg count, per-leg odds floor, total odds ceiling
  4. Entity sanity         — headline references the affected fixture's side
  5. Redundancy check      — BB legs aren't trivially equivalent

Rejections are set on the candidate itself (status=REJECTED, threshold_passed
=False, reason appended) so they still land in the candidate store for
review — the admin table can filter on status to surface what was dropped
and why.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    NewsItem,
)
from app.models.schemas import CardLeg, Game, MarketSelection

logger = logging.getLogger(__name__)

# ── Heuristic thresholds (tunable via admin review once we have labels) ──

MIN_HEADLINE_WORDS = 3
MAX_HEADLINE_WORDS = 12
MIN_ANGLE_WORDS = 6
MAX_ANGLE_WORDS = 35
MAX_BB_TOTAL_ODDS = 50.0           # >50 feels spammy / lotto-ticket territory
MIN_BB_LEG_ODDS = 1.15              # <1.15 adds zero value in a stack
MIN_BB_LEG_COUNT = 2
MAX_BB_LEG_COUNT = 5

# Forbidden phrases that indicate either wire-service voice (rewriter failed)
# or a non-story ("match preview", "matchday guide"). Order by specificity
# so the rejection reason is the most useful one.
FORBIDDEN_HEADLINE_PATTERNS = [
    (re.compile(r"^\s*(match preview|matchday guide|weekend fixtures?|weekend preview|fixtures? preview)", re.I),
     "generic headline"),
    (re.compile(r"^\s*(per sources|it was announced|confirmed today|in a press conference)", re.I),
     "wire-service headline"),
    (re.compile(r"<\s*(cite|ref|sup)\b", re.I),
     "HTML markup in headline"),
]

FORBIDDEN_ANGLE_PATTERNS = [
    (re.compile(r"<\s*(cite|ref|sup)\b", re.I), "HTML markup in angle"),
    (re.compile(r"\b(per sources|could potentially|might be expected|is said to be)\b", re.I),
     "hedgy/wire-service angle"),
]


def _word_count(s: str) -> int:
    return len((s or "").split())


def _has_proper_noun(s: str) -> bool:
    """Very loose check — something that looks like a Name or a team short-code.

    Fails on all-lowercase or generic phrases. Guards against the rewriter
    producing empty-feeling headlines.
    """
    if not s:
        return False
    # Require at least one capitalised word of length >=3 that isn't the
    # first word (first-word cap is just sentence case).
    tokens = s.split()
    for tok in tokens[1:]:
        stripped = tok.strip(",.!?—;:()\"'")
        if len(stripped) >= 3 and stripped[0].isupper() and any(c.isalpha() for c in stripped):
            return True
    # Edge: 1-word headlines fail unless it's a known abbreviation pattern.
    return False


def check_headline(headline: str) -> Optional[str]:
    """Return a rejection reason string if the headline fails, else None."""
    if not headline or not headline.strip():
        return "empty headline"
    wc = _word_count(headline)
    if wc < MIN_HEADLINE_WORDS:
        return f"headline too short ({wc} words)"
    if wc > MAX_HEADLINE_WORDS:
        return f"headline too long ({wc} words)"
    for pattern, reason in FORBIDDEN_HEADLINE_PATTERNS:
        if pattern.search(headline):
            return reason
    if not _has_proper_noun(headline):
        return "headline lacks proper noun"
    return None


def check_angle(angle: str) -> Optional[str]:
    if not angle or not angle.strip():
        return None   # angle is optional — single cards may not have one
    wc = _word_count(angle)
    if wc < MIN_ANGLE_WORDS:
        return f"angle too short ({wc} words)"
    if wc > MAX_ANGLE_WORDS:
        return f"angle too long ({wc} words)"
    for pattern, reason in FORBIDDEN_ANGLE_PATTERNS:
        if pattern.search(angle):
            return reason
    return None


def check_bet_builder(
    legs: list[CardLeg],
    total_odds: Optional[float],
) -> Optional[str]:
    n = len(legs)
    if n < MIN_BB_LEG_COUNT:
        return f"BB has only {n} leg(s)"
    if n > MAX_BB_LEG_COUNT:
        return f"BB has {n} legs (max {MAX_BB_LEG_COUNT})"

    for leg in legs:
        if not leg.odds or leg.odds < MIN_BB_LEG_ODDS:
            return f"BB leg odds too short ({leg.odds:.2f})"

    # Dedupe — each leg should reference a different market_label
    market_labels = {leg.market_label for leg in legs if leg.market_label}
    if len(market_labels) < n:
        return "BB legs aren't distinct markets"

    if total_odds is not None:
        if total_odds > MAX_BB_TOTAL_ODDS:
            return f"BB total odds too long ({total_odds:.2f})"
        if total_odds < 1.5:
            return f"BB total odds too short ({total_odds:.2f})"

    return None


def check_fixture_attribution(
    headline: str,
    angle: str,
    game: Game,
) -> Optional[str]:
    """Require the rewritten copy to reference at least one of the fixture's
    teams (full name OR short code). Catches rewriter drift that produces
    generic "great night of football" lines.
    """
    blob = f"{headline or ''} {angle or ''}".lower()
    for side in (game.home_team, game.away_team):
        name = (side.name or "").lower()
        short = (side.short_name or "").lower()
        if (name and name in blob) or (short and short in blob):
            return None
        # Also accept single-word variants of the team name (e.g. "Madrid" for "Real Madrid")
        tokens = [t for t in (side.name or "").lower().split() if len(t) >= 4]
        for t in tokens:
            if t in blob:
                return None
    return "copy doesn't reference the fixture's teams"


def apply_gates(
    candidate: CandidateCard,
    *,
    headline: str,
    angle: str,
    game: Optional[Game],
    legs: Optional[list[CardLeg]] = None,
    total_odds: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    """Evaluate all gates. Returns (passes, reason).

    `headline` and `angle` are the FINAL (post-rewriter) copy that will show
    on the card. Gate against the version the user would see, not the scout
    raw.
    """
    # Headline
    reason = check_headline(headline)
    if reason:
        return False, reason

    # Angle (only hard-fail when present and bad)
    reason = check_angle(angle)
    if reason:
        return False, reason

    # BB-specific rules
    if candidate.bet_type == BetType.BET_BUILDER:
        reason = check_bet_builder(legs or [], total_odds)
        if reason:
            return False, reason

    # Fixture attribution (guards against generic rewrites)
    if game is not None:
        reason = check_fixture_attribution(headline, angle, game)
        if reason:
            return False, reason

    return True, None
