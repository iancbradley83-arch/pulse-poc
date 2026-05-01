"""Fixture importance scorer + gradient routing helpers.

Pure-function module that computes a `[0, 1]` importance score for each
fixture from the Rogue-native signals captured in Phase 1 (PR #108):

  - `is_operator_featured` — fixture is in the operator's `getFeaturedEvents`
    response (binary discriminator)
  - `league_order` — Rogue's competition-prestige ladder, lower = higher
    operator priority (numeric ladder)

## Score formula (Phase 2b — rank-based)

    operator_signal = max(
      1.0   if game.is_operator_featured,
      league_rank_signal(game, all_games)        # rank within today's catalogue
    )
    score = operator_signal × calendar_phase_factor × intrinsic_score

`league_rank_signal` is **rank-based** within the catalogue rather than
the Phase 2a fixed `1 - league_order/100_000_000` ratio. Why: per-operator
LeagueOrder ranges differ wildly (Apuesta Total spans ~1M–64M;
hypothetical bookies might span 1k–100k). A fixed divisor is wrong for
both. Rank-based normalization just asks "how does this fixture rank
relative to today's catalogue?" — robust regardless of the underlying
numeric range.

`calendar_phase_factor` and `intrinsic_score` BOTH default to 1.0 — they're
Phase 4 work. Compute the multiplication anyway so Phase 4 can plug in
without touching call sites.

## Gradient routing (Phase 2b)

`gradient_factor(score, lo, hi)` is the load-bearing helper for routing.
Score → continuous interpolation between a floor and ceiling. Used
per-fixture for `max_searches`, candidate cap, and pre-call cost guard.
No buckets, no thresholds — `score=1.0` gets the ceiling, `score=0.0`
gets the floor, everything else gets proportional treatment.

See also:
- `~/pulse-poc/docs/fixture-importance.md` — design doc
- `feedback_calibrate_before_scoring.md` — capture-and-observe before
  behaviour-routing
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app.models.schemas import Game


# Legacy fixed-divisor normalizer — kept for back-compat with Phase 2a
# observability and tests. Production stamping uses rank-based normalizer.
_LEAGUE_ORDER_DIVISOR = 100_000_000

# Bucket thresholds — Phase 2a observability artifacts. Phase 2b routes
# via gradient_factor(); these survive only for log lines and the legacy
# classify_score() helper. Don't add new code paths that depend on them.
DEFAULT_DEEP_THRESHOLD = 0.7
DEFAULT_MINIMAL_THRESHOLD = 0.4


def compute_operator_signal(game: "Game") -> float:
    """Phase 2a operator-signal (fixed-divisor). Kept for back-compat.

    Returns max of:
      - `1.0` if the fixture is in the operator's featured-events list
      - `1.0 - (league_order / 100_000_000)` clamped to `[0, 1]`

    Production stamping uses `assign_rank_importance_scores` instead.
    """
    featured_signal = 1.0 if game.is_operator_featured else 0.0

    if game.league_order is None:
        league_signal = 0.0
    else:
        league_signal = 1.0 - (game.league_order / _LEAGUE_ORDER_DIVISOR)
        if league_signal < 0.0:
            league_signal = 0.0
        elif league_signal > 1.0:
            league_signal = 1.0

    return max(featured_signal, league_signal)


def compute_importance_score(
    game: "Game",
    calendar_phase_factor: float = 1.0,
    intrinsic_score: float = 1.0,
) -> float:
    """Phase 2a fixed-divisor score. Kept for back-compat.

    Production stamping uses `assign_rank_importance_scores`. Tests and
    isolated callers can still ask for a per-game score this way; it just
    won't see catalogue context.
    """
    operator_signal = compute_operator_signal(game)
    return operator_signal * calendar_phase_factor * intrinsic_score


def league_rank_signal(games: list["Game"]) -> dict[str, float]:
    """Rank-based league_order signal in `[0, 1]` per game.

    Sorts games by `league_order` ascending (lower = higher operator
    priority, per Rogue convention) and assigns each a rank percentile:
    rank-0 → `1.0`, rank-(n-1) → `0.0`, linearly interpolated.

    Games with `league_order is None` get `0.0` and don't participate in
    the ranking. Edge case: a single-game catalogue returns `1.0` for it.

    Returns a dict keyed on `game.id`.
    """
    by_id: dict[str, float] = {}
    rankable = [g for g in games if g.league_order is not None]
    for g in games:
        if g.league_order is None:
            by_id[g.id] = 0.0
    n = len(rankable)
    if n == 0:
        return by_id
    if n == 1:
        by_id[rankable[0].id] = 1.0
        return by_id
    ordered = sorted(
        rankable,
        key=lambda g: (g.league_order, g.id),  # id tiebreaker for determinism
    )
    for i, g in enumerate(ordered):
        by_id[g.id] = 1.0 - (i / (n - 1))
    return by_id


def assign_rank_importance_scores(
    games: list["Game"],
    *,
    calendar_phase_factor: float = 1.0,
    intrinsic_score: float = 1.0,
) -> None:
    """Phase 2b: stamp `game.importance_score` using rank-based normalization.

    Mutates each `Game` in `games` in place. Rank-percentile of
    `league_order` is computed across the catalogue passed in; featured
    fixtures still float to `1.0` regardless of league rank.

    `calendar_phase_factor` and `intrinsic_score` default to `1.0`; Phase
    4 will pass real values once those signals exist.
    """
    rank_by_id = league_rank_signal(games)
    for g in games:
        featured_signal = 1.0 if g.is_operator_featured else 0.0
        league_signal = rank_by_id.get(g.id, 0.0)
        operator_signal = max(featured_signal, league_signal)
        g.importance_score = (
            operator_signal * calendar_phase_factor * intrinsic_score
        )


def gradient_factor(score: float, lo: float, hi: float) -> float:
    """Linear interpolation: `score=0 → lo`, `score=1 → hi`.

    Score is clamped to `[0, 1]` first so a Phase 4 multiplier > 1 (e.g.
    derby boost) doesn't push the result past `hi`. Floor/ceiling
    inversion (`hi < lo`) is allowed and behaves as expected — the
    "ceiling" just means "value at score=1.0".
    """
    s = score
    if s < 0.0:
        s = 0.0
    elif s > 1.0:
        s = 1.0
    return lo + s * (hi - lo)


class GradientRoutingConfig:
    """Per-fixture gradient routing knobs (Phase 2b).

    Each knob has a floor (used at importance_score=0) and ceiling
    (importance_score=1). Linear interpolation in between via
    `gradient_factor`. `enabled=False` disables the gradient — every
    fixture gets the ceiling values, mirroring pre-Phase-2b behaviour.

    The cost cap is per-call (USD); skip the scout when the projected
    Haiku cost for the chosen `max_searches` would exceed the cap.
    `cost_cap_usd_floor=None` (or `_ceil=None`) disables the cost-cap
    guard entirely so only the global daily tripwire applies.
    """
    def __init__(
        self,
        *,
        enabled: bool,
        max_searches_floor: int,
        max_searches_ceil: int,
        per_fixture_cap_floor: int,
        per_fixture_cap_ceil: int,
        cost_cap_usd_floor: Optional[float],
        cost_cap_usd_ceil: Optional[float],
    ):
        self.enabled = enabled
        self.max_searches_floor = max_searches_floor
        self.max_searches_ceil = max_searches_ceil
        self.per_fixture_cap_floor = per_fixture_cap_floor
        self.per_fixture_cap_ceil = per_fixture_cap_ceil
        self.cost_cap_usd_floor = cost_cap_usd_floor
        self.cost_cap_usd_ceil = cost_cap_usd_ceil

    def for_score(self, score: Optional[float]) -> dict[str, Any]:
        """Return per-fixture knobs for an importance score.

        `score=None` (no Phase 1 signals stamped on this Game) is treated
        as score=1.0 — fail open to ceiling values rather than starving
        the fixture. Same when gradient is disabled.
        """
        if not self.enabled or score is None:
            return {
                "max_searches": self.max_searches_ceil,
                "per_fixture_cap": self.per_fixture_cap_ceil,
                "max_cost_usd": self.cost_cap_usd_ceil,
            }
        ms = gradient_factor(
            score, self.max_searches_floor, self.max_searches_ceil,
        )
        cap = gradient_factor(
            score, self.per_fixture_cap_floor, self.per_fixture_cap_ceil,
        )
        if self.cost_cap_usd_floor is None or self.cost_cap_usd_ceil is None:
            cost = None
        else:
            cost = gradient_factor(
                score, self.cost_cap_usd_floor, self.cost_cap_usd_ceil,
            )
        return {
            "max_searches": max(0, int(round(ms))),
            "per_fixture_cap": max(0, int(round(cap))),
            "max_cost_usd": cost,
        }


def classify_score(
    score: float,
    *,
    deep_threshold: float = DEFAULT_DEEP_THRESHOLD,
    minimal_threshold: float = DEFAULT_MINIMAL_THRESHOLD,
) -> str:
    """Phase 2a bucket helper — kept for log-line back-compat only.

    Phase 2b routes via `gradient_factor()`; this function survives so
    end-of-cycle logs can still emit a one-line distribution summary
    that's easy to skim. Don't add new behaviour paths that depend on it.
    """
    if score >= deep_threshold:
        return "deep"
    if score >= minimal_threshold:
        return "standard"
    return "minimal"
