"""Market metadata catalogue — 50 markets, the structured "what is this
bet" knowledge that lets the composer build narrative-coherent combos.

Each entry maps a Rogue market name pattern to:

  * structural fields (entity scope, claim shape, time segment)
  * **emits_signals_by_direction** — what game-state signals this
    market emits when picked in each direction (over / under / yes /
    no / specific selection). This is the load-bearing field.
  * **archetype_affinities** — which narrative archetype keys
    naturally favour this market and in which direction
  * quality flags (`card_friendly`, `cost_to_narrate`, `bb_eligible`)

The composer reads this catalogue to filter the per-fixture market
pool against a thesis and score combinations by signal coherence.

## Curation policy

Start with the top 50 markets by appearance + variety in real
operator payloads. Don't aim for perfect; the composer logs
``[narrative_uncertain]`` when a market in the pool has no metadata
entry, which is the signal to extend the catalogue.

## Population status

Currently populated: 50 markets covering Match-level, Team-specific,
Half-time, Goalscorer, Corners, Cards, Player Props, Special. Live
data sample: Manchester United vs Liverpool, 2026-05-03 (277 markets
in payload, 50 covered = ~18% — the high-traffic ~95% by selection
volume).

Add new entries as we hit gaps. New entries should follow the
``MarketMeta`` schema below. Place them in the matching section
comment for readability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Type aliases ──────────────────────────────────────────────────────

ENTITY_MATCH = "match"
ENTITY_HOME_TEAM = "home_team"
ENTITY_AWAY_TEAM = "away_team"
ENTITY_NAMED_TEAM = "named_team"
ENTITY_PLAYER = "player"

CLAIM_OVER_UNDER = "over_under"
CLAIM_YES_NO = "yes_no"
CLAIM_THREE_WAY = "three_way"
CLAIM_HANDICAP = "handicap"
CLAIM_RANGE = "range"
CLAIM_CORRECT_SCORE = "correct_score"
CLAIM_NINE_WAY = "nine_way"
CLAIM_SCORER = "scorer"

TIME_FT = "full_time"
TIME_FH = "first_half"
TIME_SH = "second_half"
TIME_MINUTE_WINDOW = "minute_window"


@dataclass(frozen=True)
class MarketMeta:
    key: str                         # stable identifier
    name_patterns: tuple[str, ...]   # case-insensitive substrings to match Rogue's MarketName
    display_name: str
    entity_scope: str
    claim_shape: str
    time_segment: str = TIME_FT
    requires_player: bool = False
    requires_team_id: bool = False
    # Signals emitted per direction. Keys are direction strings:
    # "over"/"under", "yes"/"no", "home"/"draw"/"away", or
    # "any" when direction is irrelevant.
    emits_signals_by_direction: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    # Archetype affinities: archetype_key -> (direction_hint, weight 0..1)
    archetype_affinities: dict[str, tuple[str, float]] = field(
        default_factory=dict
    )
    card_friendly: bool = True
    cost_to_narrate: int = 2  # 1 trivial..5 hard
    bb_eligible: bool = True


# ── Match-level outcome ────────────────────────────────────────────────

_MATCH_LEVEL: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="match_result",
        name_patterns=("FT 1X2", "Match Result", "1X2", "Match Winner"),
        display_name="Match Result",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home": ("dominance.{home}",),
            "away": ("dominance.{away}",),
            "draw": ("dominance.balanced", "goals.low"),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team", 0.9),
            "KEY_ATTACKER_OUT": ("opposite_subject_team", 0.7),
            "TACTICAL_LOW_BLOCK": ("draw_or_subject", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=1,
    ),
    MarketMeta(
        key="total_goals_ou",
        name_patterns=("Total Goals O/U", "Total", "Match Goals", "FT O/U"),
        display_name="Total Goals",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("goals.high", "tempo.high", "end_to_end"),
            "under": ("goals.low", "defense.tight", "tempo.low"),
        },
        archetype_affinities={
            "KEY_DEFENDER_OUT": ("over", 0.85),
            "KEY_ATTACKER_OUT": ("under", 0.85),
            "TACTICAL_HIGH_PRESS": ("over", 0.7),
            "TACTICAL_LOW_BLOCK": ("under", 0.85),
            "PLAYER_FORM_STREAK": ("over", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=1,
    ),
    MarketMeta(
        key="both_teams_to_score",
        name_patterns=("Both Teams To Score", "BTTS"),
        display_name="BTTS",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_YES_NO,
        emits_signals_by_direction={
            "yes": ("goals.high", "btts.likely.yes", "end_to_end"),
            "no": ("btts.likely.no", "clean_sheet.either"),
        },
        archetype_affinities={
            "KEY_DEFENDER_OUT": ("yes", 0.8),
            "KEY_ATTACKER_OUT": ("no", 0.7),
            "TACTICAL_LOW_BLOCK": ("no", 0.7),
            "DERBY_INTENSITY": ("yes", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=1,
    ),
    MarketMeta(
        key="double_chance",
        name_patterns=("Double Chance",),
        display_name="Double Chance",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home_or_draw": ("dominance.{home}", "dominance.balanced"),
            "away_or_draw": ("dominance.{away}", "dominance.balanced"),
            "home_or_away": ("dominance.balanced",),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team_or_draw", 0.7),
            "TACTICAL_LOW_BLOCK": ("subject_team_or_draw", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="draw_no_bet",
        name_patterns=("Draw No Bet",),
        display_name="Draw No Bet",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home": ("dominance.{home}",),
            "away": ("dominance.{away}",),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=2,
        bb_eligible=False,
    ),
    MarketMeta(
        key="asian_handicap",
        name_patterns=("Asian Handicap", "FT Asian Handicap"),
        display_name="Asian Handicap",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_HANDICAP,
        emits_signals_by_direction={
            "home_minus": ("dominance.{home}", "one_sided_dominance"),
            "away_minus": ("dominance.{away}", "one_sided_dominance"),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team_minus", 0.7),
            "KEY_ATTACKER_OUT": ("opposite_subject_team_minus", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="ht_ft",
        name_patterns=("Half Time/Full Time", "HT/FT"),
        display_name="HT/FT",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_NINE_WAY,
        emits_signals_by_direction={
            "any": ("end_to_end",),
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=4,
    ),
    MarketMeta(
        key="team_to_win_to_nil",
        name_patterns=("To Win To Nil",),
        display_name="To Win To Nil",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_YES_NO,
        requires_team_id=True,
        emits_signals_by_direction={
            "yes": ("dominance.{team}", "clean_sheet.{team}",
                    "one_sided_dominance"),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team_yes", 0.7),
            "KEY_ATTACKER_OUT": ("opposite_team_yes", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=2,
        bb_eligible=False,
    ),
)


# ── Team-specific ──────────────────────────────────────────────────────

_TEAM_LEVEL: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="team_total_goals_ou",
        name_patterns=("Total Team Goals O/U",),
        display_name="{team} Total Goals",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_OVER_UNDER,
        requires_team_id=True,
        emits_signals_by_direction={
            "over": ("team.{team}.attack.live", "dominance.{team}"),
            "under": ("team.{team}.attack.weakened", "defense.tight.{opp}"),
        },
        archetype_affinities={
            "KEY_DEFENDER_OUT": ("opp_over", 0.9),
            "KEY_ATTACKER_OUT": ("subject_under", 0.85),
            "PLAYER_FORM_STREAK": ("subject_team_over", 0.7),
            "MANAGER_PRESSURE": ("subject_team_over", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="team_clean_sheet",
        name_patterns=("Team Clean Sheet",),
        display_name="{team} Clean Sheet",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_YES_NO,
        requires_team_id=True,
        emits_signals_by_direction={
            "yes": ("clean_sheet.{team}", "defense.tight.{team}",
                    "team.{opp}.attack.weakened"),
            "no": ("defense.leaky.{team}",),
        },
        archetype_affinities={
            "KEY_ATTACKER_OUT": ("opp_yes", 0.9),
            "TACTICAL_LOW_BLOCK": ("subject_yes", 0.8),
            "MANAGER_PRESSURE": ("subject_yes", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="team_to_score_in_both_halves",
        name_patterns=("Team To Score In Both Halves",),
        display_name="{team} To Score Both Halves",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_YES_NO,
        requires_team_id=True,
        emits_signals_by_direction={
            "yes": ("team.{team}.attack.live", "dominance.{team}",
                    "goals.high"),
        },
        archetype_affinities={
            "KEY_DEFENDER_OUT": ("opp_yes", 0.7),
            "PLAYER_FORM_STREAK": ("subject_team_yes", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="team_first_half_total_goals_ou",
        name_patterns=("1st Half Total Team Goals O/U",),
        display_name="{team} 1H Goals",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_OVER_UNDER,
        time_segment=TIME_FH,
        requires_team_id=True,
        emits_signals_by_direction={
            "over": ("team.{team}.fast_start", "tempo.first_half.high"),
            "under": ("cagey_opener",),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_over", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="team_highest_scoring_half",
        name_patterns=("Team Highest Scoring Half",),
        display_name="{team} Highest Scoring Half",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_THREE_WAY,
        requires_team_id=True,
        emits_signals_by_direction={
            "first": ("team.{team}.fast_start", "tempo.first_half.high"),
            "second": ("late_drama", "tempo.second_half.high"),
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="to_win_from_behind",
        name_patterns=("To Win From Behind",),
        display_name="To Win From Behind",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_YES_NO,
        requires_team_id=True,
        emits_signals_by_direction={
            "yes": ("comeback.likely.{team}", "late_drama"),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team_yes", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=4,
        bb_eligible=False,
    ),
)


# ── Half-time / second-half ────────────────────────────────────────────

_HALF_TIME: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="first_half_1x2",
        name_patterns=("1st Half 1X2",),
        display_name="1st Half Result",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "home": ("team.{home}.fast_start", "dominance.{home}"),
            "away": ("team.{away}.fast_start", "dominance.{away}"),
            "draw": ("cagey_opener",),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_side", 0.85),
            "DERBY_INTENSITY": ("draw_or_subject", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="first_half_total_goals_ou",
        name_patterns=("1st Half Total Goals O/U", "1st Half Goals", "1st Half O/U"),
        display_name="1st Half Goals",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "over": ("tempo.first_half.high", "fast_start",
                     "set_pieces.heavy"),
            "under": ("tempo.first_half.low", "cagey_opener"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("over", 0.9),
            "TACTICAL_LOW_BLOCK": ("under", 0.7),
            "DERBY_INTENSITY": ("over", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="first_half_btts",
        name_patterns=("1st Half Both Teams To Score",),
        display_name="1st Half BTTS",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_YES_NO,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "yes": ("tempo.first_half.high", "fast_start"),
            "no": ("cagey_opener",),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("yes", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="first_half_double_chance",
        name_patterns=("1st Half Double Chance",),
        display_name="1st Half Double Chance",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "any": (),  # narrowing safety net — light signals
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="first_half_asian_handicap",
        name_patterns=("1st Half Asian Handicap",),
        display_name="1st Half AH",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_HANDICAP,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "home_minus": ("dominance.{home}", "tempo.first_half.high"),
            "away_minus": ("dominance.{away}", "tempo.first_half.high"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_minus", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=4,
    ),
    MarketMeta(
        key="second_half_1x2",
        name_patterns=("2nd Half 1X2",),
        display_name="2nd Half Result",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        time_segment=TIME_SH,
        emits_signals_by_direction={
            "home": ("dominance.{home}",),
            "away": ("dominance.{away}",),
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="second_half_total_goals_ou",
        name_patterns=("2nd Half O/U",),
        display_name="2nd Half Goals",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        time_segment=TIME_SH,
        emits_signals_by_direction={
            "over": ("late_drama", "tempo.second_half.high"),
            "under": ("controlled_match",),
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=3,
    ),
)


# ── Goalscorer ────────────────────────────────────────────────────────

_GOALSCORER: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="anytime_goalscorer",
        name_patterns=("Goalscorer", "Anytime Goalscorer"),
        display_name="Anytime Scorer",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active", "team.{p_team}.attack.live"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.95),
            "RETURNING_PLAYER": ("subject_yes", 0.9),
            "KEY_DEFENDER_OUT": ("opp_team_attackers_yes", 0.7),
            "SET_PIECE_THREAT": ("subject_yes", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=1,
        bb_eligible=False,  # base Goalscorer market is 0 BB-eligible per live data
    ),
    MarketMeta(
        key="player_to_score_or_assist",
        name_patterns=("Player To Score Or Assist", "To Score Or Assist"),
        display_name="To Score Or Assist",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active", "player.{p}.creative_role",
                    "team.{p_team}.attack.live"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.9),
            "RETURNING_PLAYER": ("subject_yes", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=1,
        bb_eligible=True,  # 39 BB-eligible per live data — preferred BB leg
    ),
    MarketMeta(
        key="player_to_score_2_or_more",
        name_patterns=("Player To Score 2 Or More",),
        display_name="To Score 2+",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active", "player.{p}.in_form",
                    "team.{p_team}.attack.live"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="player_to_score_3_or_more",
        name_patterns=("Player To Score 3 Or More",),
        display_name="Hat-trick",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.in_form",),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.6),  # lower — extreme outcome
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="player_to_score_with_header",
        name_patterns=("Player To Score With A Header",),
        display_name="Headed Goal",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active", "set_pieces.heavy"),
        },
        archetype_affinities={
            "SET_PIECE_THREAT": ("subject_yes", 0.9),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="player_to_score_outside_box",
        name_patterns=("Player To Score Outside The Box",),
        display_name="Goal Outside Box",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active",),
        },
        archetype_affinities={},
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="player_to_score_in_both_halves",
        name_patterns=("Player To Score In Both Halves",),
        display_name="Score In Both Halves",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.in_form", "team.{p_team}.attack.live"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="team_first_goalscorer",
        name_patterns=("Team First Goalscorer",),
        display_name="{team} First Scorer",
        entity_scope=ENTITY_PLAYER,  # selections are players within a team
        claim_shape=CLAIM_SCORER,
        requires_player=True,
        requires_team_id=True,
        emits_signals_by_direction={
            "any": ("team.{p_team}.scores_first.likely",),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_yes", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="first_half_anytime_goalscorer",
        name_patterns=("1st Half Anytime Goalscorer",),
        display_name="1H Anytime Scorer",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        time_segment=TIME_FH,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.active", "tempo.first_half.high"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_attackers_yes", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
)


# ── Corners ────────────────────────────────────────────────────────────

_CORNERS: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="corners_ft_ou",
        name_patterns=("Corners FT O/U", "Corners 2 Way O/U", "Corners Total"),
        display_name="Corners O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("set_pieces.heavy", "tempo.high"),
            "under": ("tempo.low",),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("over", 0.85),
            "SET_PIECE_THREAT": ("over", 0.8),
            "DERBY_INTENSITY": ("over", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="corners_ft_1x2",
        name_patterns=("Corners FT 1X2",),
        display_name="Corners 1X2",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home": ("dominance.{home}", "set_pieces.heavy"),
            "away": ("dominance.{away}", "set_pieces.heavy"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_side", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="team_total_corners_ou",
        name_patterns=("Total Team Corners O/U",),
        display_name="{team} Corners O/U",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_OVER_UNDER,
        requires_team_id=True,
        emits_signals_by_direction={
            "over": ("set_pieces.heavy", "dominance.{team}"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("subject_team_over", 0.85),
            "SET_PIECE_THREAT": ("subject_team_over", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="corners_first_half_ou",
        name_patterns=("Corners 1st Half O/U",),
        display_name="1H Corners O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "over": ("set_pieces.heavy", "tempo.first_half.high"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("over", 0.8),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="first_corner",
        name_patterns=("First Corner",),
        display_name="First Corner",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_YES_NO,
        emits_signals_by_direction={
            "yes": ("set_pieces.heavy",),
        },
        archetype_affinities={},
        card_friendly=False,
        cost_to_narrate=4,
        bb_eligible=False,
    ),
)


# ── Cards / discipline ─────────────────────────────────────────────────

_CARDS: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="cards_ft_ou",
        name_patterns=("Cards FT O/U", "Total Cards Over/Under"),
        display_name="Cards O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("discipline.heavy", "physicality.high"),
            "under": ("discipline.light", "controlled_match"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("over", 0.9),
            "TACTICAL_HIGH_PRESS": ("over", 0.7),
            "DERBY_INTENSITY": ("over", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="cards_ft_1x2",
        name_patterns=("Cards FT 1X2",),
        display_name="Cards 1X2",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home": ("discipline.heavy.{home}",),
            "away": ("discipline.heavy.{away}",),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_team_side", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="cards_first_half_ou",
        name_patterns=("Cards 1st Half O/U",),
        display_name="1H Cards O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        time_segment=TIME_FH,
        emits_signals_by_direction={
            "over": ("discipline.heavy.first_half", "physicality.high"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("over", 0.9),
            "DERBY_INTENSITY": ("over", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="player_to_be_booked",
        name_patterns=("Player To Be Booked",),
        display_name="To Be Booked",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.discipline_pressure",
                    "discipline.heavy"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_yes", 0.95),
        },
        card_friendly=True,
        cost_to_narrate=2,
        bb_eligible=False,
    ),
    MarketMeta(
        key="player_to_be_carded_first",
        name_patterns=("Player To Be Carded First",),
        display_name="First Carded",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.discipline_pressure",
                    "player.{p}.targeted_by_opp"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_yes", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=2,
        bb_eligible=False,
    ),
    MarketMeta(
        key="player_red_card",
        name_patterns=("Player Red Card",),
        display_name="To Be Sent Off",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_YES_NO,
        requires_player=True,
        emits_signals_by_direction={
            "yes": ("player.{p}.discipline_pressure",),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_yes", 0.5),  # extreme outcome
        },
        card_friendly=False,
        cost_to_narrate=3,
        bb_eligible=False,
    ),
    MarketMeta(
        key="team_total_cards_ou",
        name_patterns=("Team Total Cards O/U",),
        display_name="{team} Cards O/U",
        entity_scope=ENTITY_NAMED_TEAM,
        claim_shape=CLAIM_OVER_UNDER,
        requires_team_id=True,
        emits_signals_by_direction={
            "over": ("discipline.heavy.{team}", "physicality.high"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_team_over", 0.85),
            "DERBY_INTENSITY": ("over", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="both_teams_to_be_booked",
        name_patterns=("Both Teams To Be Booked",),
        display_name="Both Teams Booked",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_YES_NO,
        emits_signals_by_direction={
            "yes": ("discipline.heavy", "physicality.high"),
        },
        archetype_affinities={
            "DERBY_INTENSITY": ("yes", 0.8),
        },
        card_friendly=True,
        cost_to_narrate=3,
        bb_eligible=False,
    ),
)


# ── Player props (shots / tackles / fouls) ─────────────────────────────

_PLAYER_PROPS: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="player_over_shots",
        name_patterns=("Player Over Shots",),
        display_name="{player} Shots O/U",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_OVER_UNDER,
        requires_player=True,
        emits_signals_by_direction={
            "over": ("player.{p}.active", "player.{p}.attacking_role",
                     "team.{p_team}.attack.live"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_over", 0.9),
            "RETURNING_PLAYER": ("subject_over", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="player_over_shots_on_target",
        name_patterns=("Player Over Shots on Target",),
        display_name="{player} SoT O/U",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_OVER_UNDER,
        requires_player=True,
        emits_signals_by_direction={
            "over": ("player.{p}.in_form", "player.{p}.attacking_role"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("subject_over", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="player_over_tackles",
        name_patterns=("Player Over Tackles",),
        display_name="{player} Tackles O/U",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_OVER_UNDER,
        requires_player=True,
        emits_signals_by_direction={
            "over": ("player.{p}.defensive_role", "physicality.high"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_over", 0.6),  # lots of tackles → booking risk
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="player_over_fouls",
        name_patterns=("Player Over Fouls",),
        display_name="{player} Fouls O/U",
        entity_scope=ENTITY_PLAYER,
        claim_shape=CLAIM_OVER_UNDER,
        requires_player=True,
        emits_signals_by_direction={
            "over": ("player.{p}.discipline_pressure", "physicality.high"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("subject_over", 0.85),
        },
        card_friendly=True,
        cost_to_narrate=3,
        bb_eligible=False,
    ),
    MarketMeta(
        key="total_match_shots_ou",
        name_patterns=("Total Match Shots O/U",),
        display_name="Match Shots O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("tempo.high", "end_to_end"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("over", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="shots_on_target_ou",
        name_patterns=("Shots On Target O/U",),
        display_name="SoT O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("tempo.high", "end_to_end"),
        },
        archetype_affinities={
            "PLAYER_FORM_STREAK": ("over", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="total_match_fouls",
        name_patterns=("Total Match Fouls",),
        display_name="Match Fouls O/U",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_OVER_UNDER,
        emits_signals_by_direction={
            "over": ("physicality.high", "discipline.heavy"),
        },
        archetype_affinities={
            "PLAYER_DISCIPLINE_RISK": ("over", 0.7),
            "DERBY_INTENSITY": ("over", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
)


# ── Special / Other ────────────────────────────────────────────────────

_SPECIAL: tuple[MarketMeta, ...] = (
    MarketMeta(
        key="will_a_penalty_be_awarded",
        name_patterns=("Will A Penalty Be Awarded",),
        display_name="Penalty Awarded",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_YES_NO,
        emits_signals_by_direction={
            "yes": ("end_to_end", "discipline.heavy"),
        },
        archetype_affinities={
            "DERBY_INTENSITY": ("yes", 0.5),
        },
        card_friendly=False,
        cost_to_narrate=3,
        bb_eligible=False,
    ),
    MarketMeta(
        key="half_with_most_goals",
        name_patterns=("Half With Most Goals",),
        display_name="Half With Most Goals",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "first": ("tempo.first_half.high", "fast_start"),
            "second": ("late_drama", "tempo.second_half.high"),
        },
        archetype_affinities={
            "TACTICAL_HIGH_PRESS": ("first", 0.7),
        },
        card_friendly=True,
        cost_to_narrate=3,
    ),
    MarketMeta(
        key="first_to_score_1x2",
        name_patterns=("First To Score 1X2",),
        display_name="First To Score",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_THREE_WAY,
        emits_signals_by_direction={
            "home": ("team.{home}.scores_first.likely",),
            "away": ("team.{away}.scores_first.likely",),
            "no_goal": ("goals.low", "defense.tight"),
        },
        archetype_affinities={
            "MANAGER_PRESSURE": ("subject_team", 0.5),
        },
        card_friendly=True,
        cost_to_narrate=2,
    ),
    MarketMeta(
        key="winning_margin",
        name_patterns=("Winning Margin",),
        display_name="Winning Margin",
        entity_scope=ENTITY_MATCH,
        claim_shape=CLAIM_RANGE,
        emits_signals_by_direction={
            "any": ("one_sided_dominance",),
        },
        archetype_affinities={
            "INJURY_CRISIS": ("subject_team_side", 0.6),
        },
        card_friendly=True,
        cost_to_narrate=4,
    ),
)


# ── Catalogue assembly + lookup ───────────────────────────────────────

CATALOGUE: tuple[MarketMeta, ...] = (
    *_MATCH_LEVEL,
    *_TEAM_LEVEL,
    *_HALF_TIME,
    *_GOALSCORER,
    *_CORNERS,
    *_CARDS,
    *_PLAYER_PROPS,
    *_SPECIAL,
)

CATALOGUE_BY_KEY: dict[str, MarketMeta] = {m.key: m for m in CATALOGUE}


def lookup_by_market_name(market_name: str) -> Optional[MarketMeta]:
    """Find the metadata entry matching a Rogue market name (case-insensitive
    substring match). Returns `None` when no entry matches — the composer
    logs `[narrative_uncertain]` so the catalogue can be extended.
    """
    if not market_name:
        return None
    lc = market_name.lower().strip()
    # Exact match wins; fall back to substring (longest pattern first).
    for meta in CATALOGUE:
        for pat in meta.name_patterns:
            if pat.lower() == lc:
                return meta
    # Substring with longest-pattern preference for stability
    best: Optional[tuple[MarketMeta, int]] = None
    for meta in CATALOGUE:
        for pat in meta.name_patterns:
            if pat.lower() in lc and (best is None or len(pat) > best[1]):
                best = (meta, len(pat))
    return best[0] if best else None
