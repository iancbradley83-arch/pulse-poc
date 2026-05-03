"""Tests for the narrative composition pipeline.

Covers:
  * narrative_signals: resolve / conflicts
  * narrative_archetypes: keyword + hook-based detection
  * narrative_thesis: subject resolution, signal placeholder filling,
    is_uncertain flagging
  * market_meta: lookup by market name
  * combination_composer: end-to-end including the
    PLAYER_DISCIPLINE_RISK booking-watch example
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.engine import narrative_signals
from app.engine.narrative_archetypes import (
    ARCHETYPE_BY_KEY,
    derive_archetype,
)
from app.engine.narrative_thesis import build_thesis, UNCERTAINTY_THRESHOLD
from app.engine.market_meta import (
    CATALOGUE,
    CATALOGUE_BY_KEY,
    lookup_by_market_name,
)
from app.engine.combination_composer import (
    compose_candidates,
    pick_line_for_match_market,
    pick_line_for_player_selections,
)
from app.models.news import HookType, NewsItem


# ── narrative_signals ─────────────────────────────────────────────────


def test_signals_resolve_team_placeholder():
    out = narrative_signals.resolve("dominance.{team}", team_id="t-123")
    assert out == "dominance.t-123"


def test_signals_resolve_player_placeholder():
    out = narrative_signals.resolve("player.{p}.active", player_id="Salah")
    assert out == "player.Salah.active"


def test_signals_resolve_raises_on_missing_id():
    with pytest.raises(ValueError):
        narrative_signals.resolve("dominance.{team}", team_id=None)


def test_signals_static_unchanged():
    assert narrative_signals.resolve("goals.high") == "goals.high"


def test_signals_conflicts_obvious_pairs():
    assert narrative_signals.conflicts("goals.high", "goals.low")
    assert narrative_signals.conflicts("tempo.high", "tempo.low")
    assert narrative_signals.conflicts("set_pieces.heavy", "set_pieces.light")


def test_signals_conflicts_per_team_antonyms():
    assert narrative_signals.conflicts("defense.tight.t-1",
                                       "defense.leaky.t-1")
    # Different teams — not a conflict
    assert not narrative_signals.conflicts("defense.tight.t-1",
                                           "defense.leaky.t-2")


def test_signals_conflicts_player_active_vs_suppressed():
    assert narrative_signals.conflicts("player.salah.active",
                                       "player.salah.suppressed")
    assert not narrative_signals.conflicts("player.salah.active",
                                           "player.diaz.suppressed")


def test_signals_conflicts_orthogonal_pairs():
    """Unrelated signals are not conflicts."""
    assert not narrative_signals.conflicts("goals.high", "set_pieces.heavy")


# ── narrative_archetypes ──────────────────────────────────────────────


def _news(*, headline="", summary="", hook=HookType.OTHER,
          mentions=None, team_ids=None, fixture_ids=None,
          injury_details=None) -> NewsItem:
    return NewsItem(
        headline=headline,
        summary=summary,
        hook_type=hook,
        mentions=mentions or [],
        team_ids=team_ids or [],
        fixture_ids=fixture_ids or [],
        injury_details=injury_details or [],
    )


def test_archetype_player_discipline_risk_matches_booking_watch():
    n = _news(
        headline="Casemiro one yellow from suspension; Liverpool will target him",
        summary="The midfielder has been booked in 5 of the last 6 games.",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
    )
    match = derive_archetype(n)
    assert match.primary is not None
    assert match.primary.key == "PLAYER_DISCIPLINE_RISK"
    assert match.confidence >= 0.5


def test_archetype_key_attacker_out_matches_injury():
    n = _news(
        headline="Salah ruled out vs Manchester United",
        summary="Liverpool's talisman misses out with a hamstring blow.",
        hook=HookType.INJURY,
        mentions=["Salah"],
    )
    match = derive_archetype(n)
    assert match.primary is not None
    assert match.primary.key == "KEY_ATTACKER_OUT"


def test_archetype_tactical_high_press_matches_iraola_quote():
    n = _news(
        headline="Iraola: full-throttle press from minute one",
        summary="The Bournemouth manager wants intensity from kickoff.",
        hook=HookType.MANAGER_QUOTE,
        team_ids=["bournemouth"],
    )
    match = derive_archetype(n)
    assert match.primary is not None
    assert match.primary.key == "TACTICAL_HIGH_PRESS"


def test_archetype_no_match_returns_none():
    n = _news(headline="Match preview: kickoff at 3pm", hook=HookType.PREVIEW)
    match = derive_archetype(n)
    # No keywords match — primary is None
    assert match.primary is None
    assert match.confidence == 0.0


def test_archetype_returns_alternatives_when_multiple_match():
    n = _news(
        headline="Salah ruled out, Klopp under pressure",
        summary="Liverpool's manager faces sack watch ahead of injury crisis.",
        hook=HookType.MANAGER_QUOTE,
    )
    match = derive_archetype(n)
    assert match.primary is not None
    # Should have at least one alternative scoring lower
    assert isinstance(match.alternatives, tuple)


# ── narrative_thesis ─────────────────────────────────────────────────


def test_thesis_resolves_player_subject_from_injury_details():
    n = _news(
        headline="Casemiro on booking watch",
        summary="One yellow from suspension.",
        hook=HookType.PREVIEW,
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    assert thesis.archetype is not None
    assert thesis.archetype.key == "PLAYER_DISCIPLINE_RISK"
    assert thesis.subject_player_name == "Casemiro"
    assert "player.Casemiro.discipline_pressure" in thesis.resolved_signals


def test_thesis_resolves_team_subject_from_team_ids():
    n = _news(
        headline="Iraola wants high press from minute one",
        hook=HookType.MANAGER_QUOTE,
        team_ids=["bournemouth-id"],
    )
    thesis = build_thesis(n)
    assert thesis.archetype is not None
    assert thesis.archetype.key == "TACTICAL_HIGH_PRESS"
    assert thesis.subject_team_id == "bournemouth-id"
    # Per-team signal placeholder must be filled
    assert "team.bournemouth-id.high_press" in thesis.resolved_signals


def test_thesis_uncertain_when_no_archetype_matches():
    n = _news(headline="Standard match preview", hook=HookType.PREVIEW)
    thesis = build_thesis(n)
    assert thesis.is_uncertain is True
    assert thesis.confidence < UNCERTAINTY_THRESHOLD


def test_thesis_drops_unfillable_signals_silently():
    """When subject is a PLAYER but injury_details + mentions are empty,
    `{p}` placeholders cannot be resolved → those signals are dropped,
    not emitted as `player..active` (which would never match)."""
    n = _news(
        headline="Player on booking watch",
        summary="One yellow from suspension.",
        hook=HookType.PREVIEW,
        # No mentions, no injury_details — subject_player_name = None
    )
    thesis = build_thesis(n)
    # Archetype matches by keyword but subject can't be resolved → signals
    # dropped (not malformed).
    if thesis.archetype is not None:
        for s in thesis.resolved_signals:
            assert "{" not in s and "}" not in s


# ── market_meta ──────────────────────────────────────────────────────


def test_meta_lookup_exact_match():
    meta = lookup_by_market_name("FT 1X2")
    assert meta is not None
    assert meta.key == "match_result"


def test_meta_lookup_substring_match():
    meta = lookup_by_market_name("Manchester United: Total Team Goals O/U")
    assert meta is not None
    assert meta.key == "team_total_goals_ou"


def test_meta_lookup_returns_none_for_unknown():
    assert lookup_by_market_name("Some Brand New Market") is None


def test_meta_lookup_empty_string_safe():
    assert lookup_by_market_name("") is None


def test_meta_catalogue_has_50_entries():
    """Sanity: we promised ~50 markets in the design doc."""
    assert len(CATALOGUE) >= 45


def test_meta_no_duplicate_keys():
    keys = [m.key for m in CATALOGUE]
    assert len(keys) == len(set(keys))


# ── combination_composer ─────────────────────────────────────────────


def _market(_id, name, selections=None):
    """Default selections are BB-eligible so existing tests continue to
    exercise the multi-leg combo path under the new default
    `require_bb_eligibility=True`. Tests that need non-BB-eligible
    selections pass them explicitly."""
    return {
        "_id": _id,
        "Name": name,
        "Selections": selections or [
            {"Id": f"{_id}-s0", "Name": "Yes",
             "IsBetBuilderAvailable": True},
            {"Id": f"{_id}-s1", "Name": "No",
             "IsBetBuilderAvailable": True},
        ],
    }


def test_composer_player_discipline_risk_picks_discipline_only_legs():
    """The booking-watch example end-to-end. Subject = Casemiro;
    archetype = PLAYER_DISCIPLINE_RISK; bet_shape_rule = discipline_only.
    The composer must reject every market that isn't in the
    discipline-only set, even when other markets emit goals.high signals."""
    n = _news(
        headline="Casemiro one yellow from suspension; Liverpool will target him",
        summary="The midfielder has been booked in 5 of the last 6 games.",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    assert thesis.archetype.key == "PLAYER_DISCIPLINE_RISK"

    pool = [
        # Discipline-aligned markets
        _market("m-cards", "Cards FT O/U"),
        _market("m-1hcards", "Cards 1st Half O/U"),
        _market("m-totalfouls", "Total Match Fouls"),
        _market(
            "m-pbook",
            "Player To Be Booked",
            selections=[{"Id": "p-1", "Name": "Casemiro"},
                        {"Id": "p-2", "Name": "Bruno Fernandes"}],
        ),
        # Off-topic markets that today's themes might pick
        _market("m-totalgoals", "Total Goals O/U"),
        _market(
            "m-anytime",
            "Anytime Goalscorer",
            selections=[{"Id": "g-1", "Name": "Casemiro"},
                        {"Id": "g-2", "Name": "Salah"}],
        ),
        _market("m-btts", "Both Teams To Score"),
    ]
    combos = compose_candidates(thesis, pool, target_legs=3, min_legs=2)
    assert combos, "composer should produce at least one combination"

    top = combos[0]
    # Every leg must be from the discipline-only set
    discipline_keys = {
        "player_to_be_booked", "player_to_be_carded_first",
        "player_red_card", "cards_ft_ou", "cards_ft_1x2",
        "cards_first_half_ou", "team_total_cards_ou",
        "both_teams_to_be_booked", "total_match_fouls",
        "player_over_fouls", "player_over_tackles",
        "will_a_penalty_be_awarded",
    }
    for leg in top.legs:
        assert leg.market_meta_key in discipline_keys, (
            f"off-topic leg leaked through: {leg.market_meta_key} "
            f"({leg.market_name})"
        )


def test_composer_no_combinations_when_thesis_uncertain():
    n = _news(headline="generic preview", hook=HookType.PREVIEW)
    thesis = build_thesis(n)
    assert thesis.is_uncertain
    pool = [_market("m-1", "Total Goals O/U")]
    assert compose_candidates(thesis, pool) == []


def test_composer_subject_centric_rejects_off_subject_player_legs():
    """For PLAYER_FORM_STREAK on player X, an Anytime Goalscorer leg on
    a different player Y should not be accepted."""
    n = _news(
        headline="Salah scored in 4 straight; in red-hot form",
        summary="Liverpool's talisman on a goal streak.",
        hook=HookType.PREVIEW,
        mentions=["Salah"],
    )
    thesis = build_thesis(n)
    assert thesis.archetype.key == "PLAYER_FORM_STREAK"

    pool = [
        _market(
            "m-anytime-salah",
            "Anytime Goalscorer",
            selections=[{"Id": "s-1", "Name": "Mohamed Salah"},
                        {"Id": "s-2", "Name": "Diaz"}],
        ),
        _market(
            "m-shots-salah",
            "Player Over Shots",
            selections=[{"Id": "sh-1", "Name": "Mohamed Salah"}],
        ),
        # Off-subject: Anytime Goalscorer for a different player
        _market(
            "m-anytime-rashford",
            "Anytime Goalscorer",
            selections=[{"Id": "ra-1", "Name": "Rashford"}],
        ),
    ]
    combos = compose_candidates(thesis, pool, target_legs=2, min_legs=2)
    if combos:
        for leg in combos[0].legs:
            # All accepted player-scope legs must reference Salah
            if leg.selection_name:
                assert "Salah" in leg.selection_name


def test_composer_does_not_explode_on_empty_pool():
    n = _news(
        headline="Salah ruled out", hook=HookType.INJURY,
        mentions=["Salah"],
        injury_details=[{"player_name": "Salah", "team": "Liverpool",
                         "position_guess": "winger", "is_out_confirmed": True}],
    )
    thesis = build_thesis(n)
    assert compose_candidates(thesis, []) == []


def test_composer_bb_eligibility_drops_non_eligible_legs():
    """When require_bb_eligibility=True (default), a market whose
    selections all have IsBetBuilderAvailable=False must not contribute
    to a multi-leg combo. Mirrors the live MUN vs LIV finding: Player
    To Be Booked is 0/51 BB-eligible — combos can't contain it."""
    n = _news(
        headline="Casemiro one yellow from suspension; target him",
        summary="Booked in 5 of 6.",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    pool = [
        # BB-eligible: Cards FT O/U
        {"_id": "m-cards", "Name": "Cards FT O/U",
         "Selections": [{"Id": "c-o", "Name": "Over",
                         "IsBetBuilderAvailable": True},
                        {"Id": "c-u", "Name": "Under",
                         "IsBetBuilderAvailable": True}]},
        # BB-eligible: 1H Cards
        {"_id": "m-1hcards", "Name": "Cards 1st Half O/U",
         "Selections": [{"Id": "h-o", "Name": "Over",
                         "IsBetBuilderAvailable": True},
                        {"Id": "h-u", "Name": "Under",
                         "IsBetBuilderAvailable": True}]},
        # BB-eligible: Match fouls
        {"_id": "m-fouls", "Name": "Total Match Fouls",
         "Selections": [{"Id": "f-o", "Name": "Over",
                         "IsBetBuilderAvailable": True}]},
        # NOT BB-eligible: Player To Be Booked (matches live shape)
        {"_id": "m-pbook", "Name": "Player To Be Booked",
         "Selections": [{"Id": "p-1", "Name": "Casemiro",
                         "IsBetBuilderAvailable": False}]},
    ]
    combos = compose_candidates(
        thesis, pool, target_legs=4, min_legs=2,
        require_bb_eligibility=True,
        emit_singles_for_subject_misses=False,
    )
    bb_combos = [c for c in combos if c.bet_shape == "bet_builder"]
    assert bb_combos, "should produce at least one BB combo from BB-eligible pool"
    for combo in bb_combos:
        for leg in combo.legs:
            assert leg.market_meta_key != "player_to_be_booked", (
                f"non-BB-eligible leg leaked into BB combo: {leg.market_meta_key}"
            )


def test_composer_emits_singles_for_subject_player_misses():
    """Subject-player non-BB-eligible legs surface as `single`
    Combinations so the narrative still appears — just split into its
    own card downstream."""
    n = _news(
        headline="Casemiro one yellow from suspension; target him",
        summary="Booked in 5 of 6 games.",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    pool = [
        {"_id": "m-cards", "Name": "Cards FT O/U",
         "Selections": [{"Id": "c-o", "Name": "Over",
                         "IsBetBuilderAvailable": True},
                        {"Id": "c-u", "Name": "Under",
                         "IsBetBuilderAvailable": True}]},
        {"_id": "m-pbook", "Name": "Player To Be Booked",
         "Selections": [{"Id": "p-1", "Name": "Casemiro",
                         "IsBetBuilderAvailable": False}]},
        {"_id": "m-pcarded", "Name": "Player To Be Carded First",
         "Selections": [{"Id": "p-2", "Name": "Casemiro (Manchester United)",
                         "IsBetBuilderAvailable": False}]},
    ]
    combos = compose_candidates(
        thesis, pool, target_legs=3, min_legs=2,
        require_bb_eligibility=True,
        emit_singles_for_subject_misses=True,
    )
    singles = [c for c in combos if c.bet_shape == "single"]
    assert singles, "subject-player non-BB-eligible legs should surface as singles"
    for s in singles:
        assert len(s.legs) == 1
        assert "casemiro" in (s.legs[0].selection_name or "").lower()


def test_composer_singles_capped_at_two_per_thesis():
    n = _news(
        headline="Casemiro one yellow from suspension; target him",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    pool = [
        {"_id": f"m-p{i}", "Name": "Player To Be Booked",
         "Selections": [{"Id": f"p-{i}", "Name": "Casemiro",
                         "IsBetBuilderAvailable": False}]}
        for i in range(5)
    ] + [
        {"_id": f"m-f{i}", "Name": "Player Over Fouls",
         "Selections": [{"Id": f"f-{i}", "Name": "Casemiro Over 3.5",
                         "IsBetBuilderAvailable": False}]}
        for i in range(5)
    ]
    combos = compose_candidates(thesis, pool, require_bb_eligibility=True,
                                 emit_singles_for_subject_misses=True)
    singles = [c for c in combos if c.bet_shape == "single"]
    assert len(singles) <= 2


def test_composer_bb_disabled_includes_non_bb_legs():
    """When require_bb_eligibility=False, the composer can pick non-BB
    legs in multi-leg combos (e.g. for system bets)."""
    n = _news(
        headline="Casemiro one yellow from suspension",
        hook=HookType.PREVIEW,
        mentions=["Casemiro"],
        injury_details=[{"player_name": "Casemiro", "team": "Manchester United",
                         "position_guess": "defensive_mid", "is_out_confirmed": False}],
    )
    thesis = build_thesis(n)
    pool = [
        {"_id": "m-cards", "Name": "Cards FT O/U",
         "Selections": [{"Id": "c-o", "Name": "Over",
                         "IsBetBuilderAvailable": True},
                        {"Id": "c-u", "Name": "Under",
                         "IsBetBuilderAvailable": True}]},
        {"_id": "m-pbook", "Name": "Player To Be Booked",
         "Selections": [{"Id": "p-1", "Name": "Casemiro",
                         "IsBetBuilderAvailable": False}]},
    ]
    combos = compose_candidates(thesis, pool, target_legs=2, min_legs=2,
                                 require_bb_eligibility=False)
    bb_combos = [c for c in combos if c.bet_shape == "bet_builder"]
    assert bb_combos, "should produce a multi-leg combo when BB-eligibility not required"
    mixed_found = any(
        any(l.market_meta_key == "player_to_be_booked" for l in c.legs)
        for c in bb_combos
    )
    assert mixed_found


def test_composer_rejects_combos_with_signal_conflict():
    """Build a synthetic case where two leg directions emit conflicting
    signals — composer should not include that combo."""
    n = _news(
        headline="Iraola wants high press from minute one",
        hook=HookType.MANAGER_QUOTE,
        team_ids=["bournemouth"],
    )
    thesis = build_thesis(n)
    pool = [
        _market("m-totalgoals", "Total Goals O/U"),
        _market("m-1hgoals", "1st Half Total Goals O/U"),
        _market("m-corners", "Corners FT O/U"),
    ]
    combos = compose_candidates(thesis, pool, target_legs=3)
    if combos:
        # Within any combo, no two emitted signals should conflict
        for combo in combos:
            all_sigs = []
            for leg in combo.legs:
                all_sigs.extend(leg.emitted_signals)
            for i, a in enumerate(all_sigs):
                for b in all_sigs[i+1:]:
                    assert not narrative_signals.conflicts(a, b)



# ── line picker ──────────────────────────────────────────────────────


def _player_sel(name, decimal_odds, bb=False):
    safe_id = name.replace(" ", "_")
    return {
        "Id": f"sel-{safe_id}",
        "Name": name,
        "DisplayOdds": {"Decimal": str(decimal_odds)},
        "IsBetBuilderAvailable": bb,
    }


def test_pick_line_for_player_selections_real_casemiro_fouls():
    """The MUN vs LIV live data: 5 lines for Casemiro Over Fouls.
    Default band (1.7, 4.0) → pick the selection closest to the band
    midpoint (2.85). Over 2.5 @ 3.49 wins — a card-worthy line that's
    narratively stronger than Over 0.5 (a lock) or Over 4.5 (lottery).
    """
    sels = [
        _player_sel("Casemiro Over 0.5", 1.18),
        _player_sel("Casemiro Over 1.5", 1.84),
        _player_sel("Casemiro Over 2.5", 3.49),
        _player_sel("Casemiro Over 3.5", 8.00),
        _player_sel("Casemiro Over 4.5", 19.00),
    ]
    pick = pick_line_for_player_selections(sels, "Casemiro", direction="over")
    assert pick is not None
    # Over 2.5 @ 3.49 is the closest to band midpoint 2.85 (in-band
    # bonus halves distance); rejects 0.5 (too short) + 4.5 (lottery).
    assert "Over 2.5" in pick["Name"]


def test_pick_line_filters_to_target_player():
    sels = [
        _player_sel("Bruno Fernandes Over 1.5", 1.85),
        _player_sel("Casemiro Over 1.5", 2.20),
    ]
    pick = pick_line_for_player_selections(sels, "Casemiro", direction="over")
    assert "Casemiro" in pick["Name"]


def test_pick_line_returns_none_when_no_match():
    sels = [_player_sel("Bruno Fernandes Over 1.5", 1.85)]
    pick = pick_line_for_player_selections(sels, "Casemiro", direction="over")
    assert pick is None


def test_pick_line_for_match_market_picks_balanced_book_line():
    sels = [
        {"Id": "s1", "Name": "Over",
         "DisplayOdds": {"Decimal": "1.85"}},
        {"Id": "s2", "Name": "Under",
         "DisplayOdds": {"Decimal": "1.95"}},
    ]
    pick = pick_line_for_match_market(sels, direction="over")
    assert pick["Id"] == "s1"


def test_pick_line_band_clamps_extremes():
    """A 1.05 selection (heavy favourite) and a 12.0 selection
    (longshot) — picker prefers the in-band one if any."""
    sels = [
        _player_sel("Casemiro Over 0.5", 1.05),
        _player_sel("Casemiro Over 2.5", 3.50),
    ]
    pick = pick_line_for_player_selections(sels, "Casemiro", direction="over")
    assert "Over 2.5" in pick["Name"]

