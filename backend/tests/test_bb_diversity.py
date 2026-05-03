"""Tests for Phase 3b BB/combo diversity reporter.

Pure-function module. Verifies odds bucketing boundaries, leg-count
classification (singles always count as 1), composition reporting
math, and the per-importance target distributions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.engine.bb_diversity import (  # noqa: E402
    bucket_for_odds,
    composition_report,
    format_composition_log_line,
    leg_count_for_card,
    target_leg_distribution,
    target_odds_distribution,
)
from app.models.news import BetType, CandidateCard, HookType


def _card(*, bet_type=BetType.SINGLE, selection_ids=None, total_odds=None,
          hook=HookType.OTHER):
    return CandidateCard(
        bet_type=bet_type,
        selection_ids=selection_ids or [],
        total_odds=total_odds,
        hook_type=hook,
    )


# ── bucket_for_odds ───────────────────────────────────────────────────


def test_bucket_for_odds_short():
    assert bucket_for_odds(1.20) == "short"
    assert bucket_for_odds(1.49) == "short"


def test_bucket_for_odds_mid():
    assert bucket_for_odds(1.50) == "mid"
    assert bucket_for_odds(2.00) == "mid"
    assert bucket_for_odds(2.49) == "mid"


def test_bucket_for_odds_plus():
    assert bucket_for_odds(2.50) == "plus"
    assert bucket_for_odds(4.99) == "plus"


def test_bucket_for_odds_long():
    assert bucket_for_odds(5.00) == "long"
    assert bucket_for_odds(11.99) == "long"


def test_bucket_for_odds_lottery():
    assert bucket_for_odds(12.00) == "lottery"
    assert bucket_for_odds(50.0) == "lottery"


def test_bucket_for_odds_unknown_for_invalid():
    assert bucket_for_odds(None) == "unknown"
    assert bucket_for_odds(0.95) == "unknown"     # impossible decimal odds
    assert bucket_for_odds(1.0) == "unknown"      # break-even
    assert bucket_for_odds("nope") == "unknown"   # type-check defence


# ── leg_count_for_card ────────────────────────────────────────────────


def test_leg_count_single_is_one():
    c = _card(bet_type=BetType.SINGLE, selection_ids=[])
    assert leg_count_for_card(c) == 1


def test_leg_count_bet_builder_uses_selection_ids():
    c = _card(bet_type=BetType.BET_BUILDER, selection_ids=["a", "b", "c"])
    assert leg_count_for_card(c) == 3


def test_leg_count_combo_uses_selection_ids():
    c = _card(bet_type=BetType.COMBO, selection_ids=["a", "b"])
    assert leg_count_for_card(c) == 2


def test_leg_count_zero_selections_falls_back_to_one():
    """A misshapen BB with no selections falls back to 1 to keep the
    histogram non-zero and locatable."""
    c = _card(bet_type=BetType.BET_BUILDER, selection_ids=[])
    assert leg_count_for_card(c) == 1


# ── target distributions ──────────────────────────────────────────────


def test_target_leg_distribution_top_fixture_widens_spread():
    top = target_leg_distribution(1.0)
    # Top fixture has non-zero share at 4, 5, 6 legs
    assert top[4] > 0
    assert top[5] > 0
    assert top[6] > 0


def test_target_leg_distribution_tail_fixture_concentrates():
    tail = target_leg_distribution(0.0)
    # Tail fixture has zero share at 4+ legs
    assert tail[4] == 0
    assert tail[5] == 0
    assert tail[6] == 0
    # Most weight at singles + 2-leg
    assert tail[1] + tail[2] >= 0.7


def test_target_distributions_sum_to_one():
    for s in (0.0, 0.5, 1.0):
        legs = target_leg_distribution(s)
        odds = target_odds_distribution(s)
        assert abs(sum(legs.values()) - 1.0) < 0.01
        assert abs(sum(odds.values()) - 1.0) < 0.01


def test_target_distributions_handle_none_score():
    """`None` score (no Phase 1 signal) fails open to ceiling — the
    same convention as `cap_for_score(None)`."""
    legs = target_leg_distribution(None)
    assert legs[5] > 0  # ceiling-style spread


# ── composition_report ───────────────────────────────────────────────


def test_composition_report_counts_bet_types_and_legs():
    cards = [
        _card(bet_type=BetType.SINGLE, total_odds=1.85),
        _card(bet_type=BetType.SINGLE, total_odds=1.40),
        _card(bet_type=BetType.BET_BUILDER, selection_ids=["a", "b"], total_odds=3.20),
        _card(bet_type=BetType.BET_BUILDER, selection_ids=["a", "b", "c"], total_odds=6.80),
        _card(bet_type=BetType.COMBO, selection_ids=["a", "b"], total_odds=4.50),
    ]
    r = composition_report(cards)
    assert r["total_cards"] == 5
    assert r["by_bet_type"]["single"] == 2
    assert r["by_bet_type"]["bet_builder"] == 2
    assert r["by_bet_type"]["combo"] == 1
    assert r["by_leg_count"][1] == 2
    assert r["by_leg_count"][2] == 2
    assert r["by_leg_count"][3] == 1
    assert r["bb_or_combo_by_leg_count"][2] == 2
    assert r["bb_or_combo_by_leg_count"][3] == 1


def test_composition_report_buckets_by_odds():
    cards = [
        _card(total_odds=1.30),  # short
        _card(total_odds=1.85),  # mid
        _card(total_odds=2.00),  # mid
        _card(total_odds=4.50),  # plus
        _card(total_odds=8.00),  # long
        _card(total_odds=20.0),  # lottery
        _card(total_odds=None),  # unknown
    ]
    r = composition_report(cards)
    assert r["by_odds_bucket"]["short"] == 1
    assert r["by_odds_bucket"]["mid"] == 2
    assert r["by_odds_bucket"]["plus"] == 1
    assert r["by_odds_bucket"]["long"] == 1
    assert r["by_odds_bucket"]["lottery"] == 1
    assert r["by_odds_bucket"].get("unknown", 0) == 1


def test_composition_report_odds_range_min_max():
    cards = [
        _card(total_odds=1.40),
        _card(total_odds=8.50),
        _card(total_odds=2.00),
        _card(total_odds=None),  # excluded from range
    ]
    r = composition_report(cards)
    assert r["odds_range"]["min"] == 1.40
    assert r["odds_range"]["max"] == 8.50
    assert r["odds_range"]["with_odds"] == 3


def test_composition_report_empty_input_safe():
    r = composition_report([])
    assert r["total_cards"] == 0
    assert r["odds_range"]["min"] is None
    assert r["odds_range"]["max"] is None


# ── format_composition_log_line ──────────────────────────────────────


def test_format_log_line_includes_key_fields():
    cards = [
        _card(bet_type=BetType.SINGLE, total_odds=1.85),
        _card(bet_type=BetType.BET_BUILDER, selection_ids=["a", "b", "c"], total_odds=4.20),
    ]
    line = format_composition_log_line(composition_report(cards))
    assert "total=2" in line
    assert "single=1" in line
    assert "bet_builder=1" in line
    assert "1=1" in line  # leg count 1
    assert "3=1" in line  # leg count 3


def test_format_log_line_handles_no_odds_data():
    cards = [_card(total_odds=None)]
    line = format_composition_log_line(composition_report(cards))
    assert "odds_range=n/a" in line
