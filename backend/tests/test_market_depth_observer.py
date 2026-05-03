"""Tests for Phase 3a market-depth observer.

Pure-function module: no live HTTP, no DB, no LLM. Verifies that we
correctly walk Rogue's `InMarketGroups` / `MarketGroupOrder` shapes (both
dict-form per the OpenAPI spec and the bare-string fallback we've seen
on some endpoints) and produce a sensible per-group distribution.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_market_depth_observer.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.market_depth_observer import (  # noqa: E402
    _coerce_group_entry,
    _group_distribution,
    observe_fixture,
    observe_top_fixtures,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _market(name: str, in_groups, market_order=None):
    return {
        "MarketName": name,
        "InMarketGroups": in_groups,
        "MarketGroupOrder": market_order,
    }


def _event(*, event_id="evt-1", participants=None, markets=None,
           total_count=None, bb_enabled=False, market_groups=None):
    if participants is None:
        participants = [{"Name": "Arsenal"}, {"Name": "Real Madrid"}]
    if markets is None:
        markets = []
    return {
        "_id": event_id,
        "Participants": participants,
        "Markets": markets,
        "TotalActiveMarketsCount": (
            total_count if total_count is not None else len(markets)
        ),
        "Settings": {"IsBetBuilderEnabled": bb_enabled},
        "MarketGroups": market_groups or [],
    }


class _FakeGame:
    """Stand-in for Game so we don't need to import the pydantic model."""
    def __init__(self, *, fixture_id, score, league_order=None, featured=False):
        self.id = fixture_id
        self.importance_score = score
        self.league_order = league_order
        self.is_operator_featured = featured


# ── _coerce_group_entry ────────────────────────────────────────────────


def test_coerce_group_entry_string_uses_market_order():
    name, order = _coerce_group_entry("Goals", 14.0)
    assert name == "Goals"
    assert order == 14.0


def test_coerce_group_entry_dict_with_own_order_overrides():
    name, order = _coerce_group_entry({"Name": "Goals", "Order": 3}, 99.0)
    assert name == "Goals"
    assert order == 3.0


def test_coerce_group_entry_live_shape_uses_market_order():
    """Regression: the LIVE Rogue shape uses `MarketOrder` (not `Order`),
    verified 2026-05-03 against MUN vs LIV. Without this fix, the
    per-group rank silently fell back to the market-level GLOBAL
    `MarketGroupOrder` (e.g. 161 instead of the per-group 0-28), making
    the order ranges in the observer log meaningless. `MarketOrder` must
    be checked first.
    """
    name, order = _coerce_group_entry(
        {
            "_id": "g-goals", "Name": "Goals",
            "MarketOrder": 14, "SortingKey": "x",
        },
        fallback_order=99999.0,
    )
    assert name == "Goals"
    assert order == 14.0


def test_coerce_group_entry_per_group_rank_beats_market_level_fallback():
    """Per-group MarketOrder must beat the market-level fallback even
    when the fallback is also numeric — the market-level number is a
    global rank that's wrong for per-group sampling."""
    name, order = _coerce_group_entry(
        {"Name": "Corners", "MarketOrder": 2}, fallback_order=161.0,
    )
    assert order == 2.0


def test_coerce_group_entry_dict_no_order_falls_back_to_market_order():
    name, order = _coerce_group_entry({"Name": "Corners"}, 7.0)
    assert name == "Corners"
    assert order == 7.0


def test_coerce_group_entry_dict_alt_name_keys():
    name, _ = _coerce_group_entry({"GroupName": "Players"}, None)
    assert name == "Players"
    name, _ = _coerce_group_entry({"name": "Cards"}, None)
    assert name == "Cards"


def test_coerce_group_entry_no_name_returns_none():
    name, _ = _coerce_group_entry({"NoNameField": "x"}, 1.0)
    assert name is None


# ── _group_distribution ────────────────────────────────────────────────


def test_group_distribution_counts_per_membership():
    """A market in two groups counts once per group."""
    markets = [
        _market("MR", ["Main"], 1),
        _market("BTTS", ["Main", "Goals"], 5),
        _market("OU 2.5", ["Main", "Goals"], 2),
    ]
    dist = _group_distribution(markets)
    assert dist["Main"]["market_count"] == 3
    assert dist["Goals"]["market_count"] == 2
    assert dist["Main"]["order_min"] == 1
    assert dist["Main"]["order_max"] == 5
    assert dist["Goals"]["order_min"] == 2
    assert dist["Goals"]["order_max"] == 5


def test_group_distribution_handles_dict_entries_with_order():
    """Spec lists InMarketGroups as objects with their own Order field."""
    markets = [
        _market("M1", [{"Name": "Goals", "Order": 4}], None),
        _market("M2", [{"Name": "Goals", "Order": 11}], None),
        _market("M3", [{"Name": "Goals", "Order": 22}], None),
    ]
    dist = _group_distribution(markets)
    assert dist["Goals"]["market_count"] == 3
    assert dist["Goals"]["ranked_market_count"] == 3
    assert dist["Goals"]["order_min"] == 4
    assert dist["Goals"]["order_max"] == 22
    assert dist["Goals"]["order_median"] == 11.0


def test_group_distribution_unranked_markets_counted_separately():
    """Markets without any order field still contribute to count."""
    markets = [
        _market("M1", ["Specials"], 3),
        _market("M2", ["Specials"], None),
        _market("M3", ["Specials"], None),
    ]
    dist = _group_distribution(markets)
    assert dist["Specials"]["market_count"] == 3
    assert dist["Specials"]["ranked_market_count"] == 1
    assert dist["Specials"]["unranked_market_count"] == 2


def test_group_distribution_empty_markets_returns_empty_dict():
    assert _group_distribution([]) == {}


def test_group_distribution_skips_markets_with_no_groups():
    markets = [_market("Orphan", [], 5)]
    assert _group_distribution(markets) == {}


# ── observe_fixture ────────────────────────────────────────────────────


def test_observe_fixture_full_shape():
    event = _event(
        markets=[
            _market("MR", ["Main"], 1),
            _market("Corners O/U 9.5", ["Corners"], 14),
        ],
        total_count=396,
        bb_enabled=True,
        market_groups=["Main", "Halves", "Goals", "Corners"],
    )
    g = _FakeGame(fixture_id="evt-1", score=1.0, league_order=1_000_001,
                  featured=True)
    report = observe_fixture(event, game=g)
    assert report["event_id"] == "evt-1"
    assert report["label"] == "Arsenal vs Real Madrid"
    assert report["importance_score"] == 1.0
    assert report["league_order"] == 1_000_001
    assert report["is_operator_featured"] is True
    assert report["total_active_markets_count"] == 396
    assert report["is_bet_builder_enabled"] is True
    assert report["groups_present"] == ["Corners", "Goals", "Halves", "Main"]
    assert "Main" in report["groups_detail"]
    assert "Corners" in report["groups_detail"]


def test_observe_fixture_without_game_uses_event_fields():
    """Admin endpoint path: no Game in memory, fall back to event fields."""
    event = _event()
    event["LeagueOrder"] = 7_000_002
    report = observe_fixture(event, game=None)
    assert report["importance_score"] is None
    assert report["league_order"] == 7_000_002
    assert report["is_operator_featured"] is None


def test_observe_fixture_handles_missing_participants():
    event = _event(participants=[])
    report = observe_fixture(event, game=None)
    assert report["label"] == "? vs ?"


# ── observe_top_fixtures ───────────────────────────────────────────────


def test_observe_top_fixtures_picks_highest_score(caplog):
    games = [
        _FakeGame(fixture_id="low", score=0.1),
        _FakeGame(fixture_id="hi", score=0.9),
        _FakeGame(fixture_id="mid", score=0.5),
    ]
    raw_by_id = {
        "low": _event(event_id="low",
                      participants=[{"Name": "L1"}, {"Name": "L2"}]),
        "hi": _event(event_id="hi",
                     participants=[{"Name": "H1"}, {"Name": "H2"}]),
        "mid": _event(event_id="mid",
                      participants=[{"Name": "M1"}, {"Name": "M2"}]),
    }
    reports = observe_top_fixtures(games, raw_by_id, top_n=2)
    assert [r["event_id"] for r in reports] == ["hi", "mid"]


def test_observe_top_fixtures_skips_games_without_raw_payload(caplog):
    games = [_FakeGame(fixture_id="present", score=0.9),
             _FakeGame(fixture_id="missing", score=0.8)]
    raw_by_id = {"present": _event(event_id="present")}
    reports = observe_top_fixtures(games, raw_by_id, top_n=5)
    assert len(reports) == 1
    assert reports[0]["event_id"] == "present"


def test_observe_top_fixtures_empty_inputs_no_crash():
    assert observe_top_fixtures([], {}, top_n=3) == []
    assert observe_top_fixtures([_FakeGame(fixture_id="x", score=1.0)], {},
                                 top_n=3) == []


def test_observe_top_fixtures_handles_none_score():
    """A Game with importance_score=None must not crash the sort."""
    games = [
        _FakeGame(fixture_id="scored", score=0.5),
        _FakeGame(fixture_id="unscored", score=None),
    ]
    raw_by_id = {
        "scored": _event(event_id="scored"),
        "unscored": _event(event_id="unscored"),
    }
    reports = observe_top_fixtures(games, raw_by_id, top_n=2)
    # Both should appear; scored first.
    assert reports[0]["event_id"] == "scored"
    assert len(reports) == 2
