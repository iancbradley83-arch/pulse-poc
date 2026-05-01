"""Fixture importance scorer — Phase 2a (observability only, no routing).

Pure-function module that computes a `[0, 1]` importance score for each
fixture from the Rogue-native signals captured in Phase 1 (PR #108):

  - `is_operator_featured` — fixture is in the operator's `getFeaturedEvents`
    response (binary discriminator)
  - `league_order` — Rogue's competition-prestige ladder, lower = higher
    operator priority (numeric ladder)

The score formula (per `~/pulse-poc/docs/fixture-importance.md`,
post-calibration):

    operator_signal = max(
      1.0   if game.is_operator_featured,
      1.0 - (game.league_order / 100_000_000)    # normalized inverse-rank
    )
    score = operator_signal × calendar_phase_factor × intrinsic_score

`calendar_phase_factor` and `intrinsic_score` BOTH default to 1.0 today —
they're Phase 4 work. Compute the multiplication anyway so Phase 2b/4 can
plug them in cleanly without touching call sites.

`classify_score` puts a score into a tier bucket — `"deep"`, `"standard"`,
or `"minimal"`. **Computed and logged in Phase 2a but NOT consumed by the
tier loops yet.** Phase 2b will route fixtures based on these buckets
after we calibrate against live data.

See also:
- `~/pulse-poc/docs/fixture-importance.md` — design doc
- `feedback_calibrate_before_scoring.md` — why we capture-and-observe
  before behaviour-routing
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.schemas import Game


# Normalization divisor for `league_order`. Rogue's ladder runs from
# ~1_000_001 (highest priority, e.g. UCL) to ~64_000_000+ (low priority,
# e.g. CONCACAF Champions Cup on Apuesta Total). 100M gives clean headroom
# and means the smallest league_order yields ~0.99, the largest ~0.36.
_LEAGUE_ORDER_DIVISOR = 100_000_000

# Default tier thresholds — match the design doc and acceptance criteria.
# Phase 2b will lift these into env vars once they actually drive routing.
DEFAULT_DEEP_THRESHOLD = 0.7
DEFAULT_MINIMAL_THRESHOLD = 0.4


def compute_operator_signal(game: "Game") -> float:
    """Operator-side importance signal in `[0, 1]`.

    Returns the max of:
      - `1.0` if the fixture is in the operator's featured-events list
      - `1.0 - (league_order / 100_000_000)` clamped to `[0, 1]`

    Returns `0.0` when neither signal is available (no `is_operator_featured`,
    no `league_order`).
    """
    featured_signal = 1.0 if game.is_operator_featured else 0.0

    if game.league_order is None:
        league_signal = 0.0
    else:
        # Inverse-rank: lower league_order = higher signal. Clamp to
        # [0, 1] so a hypothetical league_order > 100M can't go negative.
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
    """Final importance score in `[0, ?]` — see formula in module docstring.

    `calendar_phase_factor` and `intrinsic_score` default to `1.0` in Phase
    2a; the formula reduces to `operator_signal`. Phase 4 will plug in
    real multipliers (knockout/title race/relegation; derbies, etc.).

    `intrinsic_score > 1.0` is allowed by design — a derby boost can lift
    a fixture above its baseline operator signal.
    """
    operator_signal = compute_operator_signal(game)
    return operator_signal * calendar_phase_factor * intrinsic_score


def classify_score(
    score: float,
    *,
    deep_threshold: float = DEFAULT_DEEP_THRESHOLD,
    minimal_threshold: float = DEFAULT_MINIMAL_THRESHOLD,
) -> str:
    """Bucket a score into `"deep"`, `"standard"`, or `"minimal"`.

    Bucketing rules (closed-open intervals, matching the design doc):
      - score >= deep_threshold      -> "deep"
      - minimal_threshold <= score < deep_threshold -> "standard"
      - score < minimal_threshold    -> "minimal"

    **Phase 2a: computed and logged, NOT consumed by tier loops.** Phase
    2b will route fixtures to deep/standard/minimal scout modes based on
    this bucket.
    """
    if score >= deep_threshold:
        return "deep"
    if score >= minimal_threshold:
        return "standard"
    return "minimal"
