"""Cross-event builder — turns a StorylineItem into a multi-event CandidateCard.

Resolves each storyline participant to a real fixture in the current
catalogue, picks one aligned market leg per fixture (e.g. anytime-scorer
for a Golden Boot storyline, opponent-to-win for a relegation at-risk
side), and emits a `CandidateCard` with `bet_type=BetType.COMBO` and
populated `selection_ids`. The post-engine `calculate_bets` sweep in
`main.py` stamps the real combo price.

Pricing validation (is this combo legal across events?) happens downstream
via `calculate_bets` returning a `Bets[Type='Combo']` entry. Unlike
same-event Bet Builders, cross-event combos don't need `betbuilder/match`
— the operator allows any cross-event permutation at the combined price.

Per-storyline leg picking:
  - GOLDEN_BOOT: anytime goalscorer for the named striker.
  - RELEGATION: opponent-to-win (match_result) for the at-risk team's
    fixture; fall back to under 2.5 total goals if no match_result
    available. Relegation scraps are historically low-scoring, and backing
    the favourite/opponent is the natural way to "play the storyline".
  - EUROPE_CHASE: team-to-win (match_result) for the chasing side's
    fixture; fall back to over 2.5 total goals if no match_result
    available. Clubs pushing for Europe are attacking; backing them to win
    is the natural storyline bet.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.engine.candidate_builder import _match_player_selection
from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    HookType,
    StorylineItem,
    StorylineParticipant,
    StorylineType,
)
from app.models.schemas import Game, Market, MarketSelection
from app.services.market_catalog import MarketCatalog

logger = logging.getLogger(__name__)


# Minimum / maximum legs in a cross-event combo. Leg count is driven by
# narrative fit (how many chasers are actually playing this matchweek),
# not a fixed constant — if 5 Golden Boot contenders are in action, emit
# 5 legs; if only 2 are playing, emit 2. 2 is the floor for anything to
# feel like a "combo" at all; 6 is the ceiling past which the odds get
# absurd and the story dilutes.
MIN_COMBO_LEGS = 2
MAX_COMBO_LEGS = 6


def _lower_name(name: str) -> str:
    return (name or "").strip().lower()


def _find_fixture_for_team(
    team_name: str, games: dict[str, Game]
) -> Optional[tuple[Game, str]]:
    """Fuzzy match a team name (as the LLM wrote it) to a fixture.

    Returns (game, side) where side is "home" or "away" — the relegation
    / europe-chase pickers need to know which side of the fixture the
    participating team is on. Returns None if no match.

    Match strategy: exact match on full name first, then exact match on
    short name, then substring. First hit wins — the catalogue typically
    has <=25 fixtures in a single cycle so collisions are rare.
    """
    wanted = _lower_name(team_name)
    if not wanted:
        return None
    for strategy in ("exact_name", "exact_short", "substring_name"):
        for g in games.values():
            home = _lower_name(g.home_team.name)
            away = _lower_name(g.away_team.name)
            home_short = _lower_name(g.home_team.short_name)
            away_short = _lower_name(g.away_team.short_name)
            if strategy == "exact_name":
                if wanted == home:
                    return g, "home"
                if wanted == away:
                    return g, "away"
            elif strategy == "exact_short":
                if wanted == home_short:
                    return g, "home"
                if wanted == away_short:
                    return g, "away"
            elif strategy == "substring_name":
                if wanted in home or home in wanted:
                    return g, "home"
                if wanted in away or away in wanted:
                    return g, "away"
    return None


def _pick_goalscorer_leg(
    catalog: MarketCatalog, game: Game, player_name: str
) -> Optional[tuple[Market, MarketSelection]]:
    """Find the anytime-scorer selection matching `player_name` in this fixture's Goalscorer market."""
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == "goalscorer"]
    if not markets:
        return None
    market = markets[0]
    sel = _match_player_selection(market, [player_name])
    if sel is None or not sel.selection_id:
        return None
    return market, sel


def _pick_match_result_leg(
    catalog: MarketCatalog, game: Game, side: str
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick the home / away selection from the match_result (1X2) market.

    `side` must be "home" or "away". Returns None if no match_result
    market or no matching selection with a Rogue selection_id.
    """
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == "match_result"]
    if not markets:
        return None
    market = markets[0]
    wanted = (side or "").lower()
    if wanted not in ("home", "away"):
        return None
    for sel in market.selections:
        if (sel.outcome_type or "").lower() == wanted and sel.selection_id:
            return market, sel
    return None


def _pick_totals_leg(
    catalog: MarketCatalog, game: Game, direction: str
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick the over / under selection from the main over_under (total
    goals) market. `direction` must be "over" or "under". Returns None if
    no over_under market or no matching selection with a selection_id.
    """
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == "over_under"]
    if not markets:
        return None
    market = markets[0]
    wanted = (direction or "").lower()
    if wanted not in ("over", "under"):
        return None
    for sel in market.selections:
        if (sel.outcome_type or "").lower() == wanted and sel.selection_id:
            return market, sel
    return None


def _pick_relegation_leg(
    catalog: MarketCatalog, game: Game, at_risk_side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a relegation-aligned leg for the at-risk team's fixture.

    Primary: opponent to win (1X2). Fallback: under 2.5 total goals
    (relegation scraps historically low-scoring). Returns None if neither
    works.
    """
    opponent_side = "away" if at_risk_side == "home" else "home"
    picked = _pick_match_result_leg(catalog, game, opponent_side)
    if picked is not None:
        return picked
    return _pick_totals_leg(catalog, game, "under")


def _pick_europe_chase_leg(
    catalog: MarketCatalog, game: Game, chaser_side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a Europe-chase-aligned leg for the chasing team's fixture.

    Primary: chasing team to win (1X2). Fallback: over 2.5 total goals
    (teams pushing for Europe are attacking). Returns None if neither.
    """
    picked = _pick_match_result_leg(catalog, game, chaser_side)
    if picked is not None:
        return picked
    return _pick_totals_leg(catalog, game, "over")


def _pick_btts_yes_leg(
    catalog: MarketCatalog, game: Game,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick the "Yes" selection from the btts market for DERBY_WEEKEND.

    BTTS is the canonical derby bet — rivalry + stakes + familiarity
    between XIs reliably produces goals at both ends. Returns None if
    the fixture has no btts market.
    """
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == "btts"]
    if not markets:
        return None
    market = markets[0]
    # btts selections carry outcome_type "yes" / "no" post-catalogue-load.
    for sel in market.selections:
        if (sel.outcome_type or "").lower() == "yes" and sel.selection_id:
            return market, sel
    # Some ingested btts markets use label-only; last-resort match.
    for sel in market.selections:
        if "yes" in (sel.label or "").lower() and sel.selection_id:
            return market, sel
    return None


def _pick_derby_weekend_leg(
    catalog: MarketCatalog, game: Game, _side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a derby-aligned leg — BTTS Yes primary, Over 2.5 fallback.

    `side` is ignored: DERBY_WEEKEND doesn't back either team, it backs
    the FIXTURE to be eventful. Keeping the signature identical to the
    other pickers lets the dispatch table stay uniform.
    """
    picked = _pick_btts_yes_leg(catalog, game)
    if picked is not None:
        return picked
    return _pick_totals_leg(catalog, game, "over")


def _pick_european_week_leg(
    catalog: MarketCatalog, game: Game, club_side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a European-week-aligned leg — club to win (1X2), fallback
    over 2.5 goals. Same shape as Europe Chase since the "back the
    narrative" intent is identical.
    """
    picked = _pick_match_result_leg(catalog, game, club_side)
    if picked is not None:
        return picked
    return _pick_totals_leg(catalog, game, "over")


def _pick_title_race_leg(
    catalog: MarketCatalog, game: Game, contender_side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a title-race-aligned leg — contender to win their match.

    No fallback to totals — if a title contender's fixture is missing
    1X2 odds something is off with the catalogue, skip the leg rather
    than corrupt the "all to win" framing with a goals bet.
    """
    return _pick_match_result_leg(catalog, game, contender_side)


def _pick_home_fortress_leg(
    catalog: MarketCatalog, game: Game, _side: str,
) -> Optional[tuple[Market, MarketSelection]]:
    """Pick a fortress-aligned leg — home side to win. Always 'home'
    regardless of the side value (the scout only emits home-side
    participants for this type).
    """
    return _pick_match_result_leg(catalog, game, "home")


_SUPPORTED_TYPES: set[StorylineType] = {
    StorylineType.GOLDEN_BOOT,
    StorylineType.RELEGATION,
    StorylineType.EUROPE_CHASE,
    StorylineType.TITLE_RACE,
    StorylineType.DERBY_WEEKEND,
    StorylineType.EUROPEAN_WEEK,
    StorylineType.HOME_FORTRESS,
    StorylineType.GOAL_MACHINES,
}

# Types whose leg is a player-anytime-goalscorer pick — they need a
# player_name on the participant (GOLDEN_BOOT's original guard is now
# shared with GOAL_MACHINES).
_PLAYER_LEG_TYPES: set[StorylineType] = {
    StorylineType.GOLDEN_BOOT,
    StorylineType.GOAL_MACHINES,
}


class CrossEventBuilder:
    """StorylineItem -> CandidateCard(COMBO).

    Supports the three original types (GOLDEN_BOOT, RELEGATION,
    EUROPE_CHASE) plus the five expansion types (TITLE_RACE,
    DERBY_WEEKEND, EUROPEAN_WEEK, HOME_FORTRESS, GOAL_MACHINES). Each
    type has a dedicated leg-picker; types not in _SUPPORTED_TYPES
    (reserved enum values like MANAGER_PRESSURE / DEBUT_RETURN) return
    None.
    """

    def __init__(self, catalog: MarketCatalog):
        self._catalog = catalog

    def build(
        self, storyline: StorylineItem, games: dict[str, Game]
    ) -> Optional[CandidateCard]:
        st = storyline.storyline_type
        if st not in _SUPPORTED_TYPES:
            logger.debug(
                "CrossEventBuilder: type=%s not supported", st.value,
            )
            return None

        legs: list[tuple[Market, MarketSelection]] = []
        resolved_participants: list[StorylineParticipant] = []
        seen_fixture_ids: set[str] = set()

        for p in storyline.participants:
            if len(legs) >= MAX_COMBO_LEGS:
                break
            if not p.team_name:
                logger.debug(
                    "CrossEventBuilder: skipping participant with missing team: %s", p,
                )
                continue
            # Player-leg types (GOLDEN_BOOT, GOAL_MACHINES) need a player
            # name — the leg is a goalscorer selection. Team-leg types
            # (relegation, europe chase, title race, derby weekend,
            # european week, home fortress) don't.
            if st in _PLAYER_LEG_TYPES and not p.player_name:
                logger.debug(
                    "CrossEventBuilder: %s participant missing player_name: %s",
                    st.value, p,
                )
                continue
            match = _find_fixture_for_team(p.team_name, games)
            if match is None:
                logger.info(
                    "CrossEventBuilder: team %r not in catalogue — skipping participant %s",
                    p.team_name, p.player_name or p.team_name,
                )
                continue
            game, side = match
            if game.id in seen_fixture_ids:
                # Two storyline participants playing in the same fixture —
                # we only want one leg per fixture so the combo stays
                # cross-event. Drop the second.
                logger.debug(
                    "CrossEventBuilder: duplicate fixture %s for storyline — skipping %s",
                    game.id, p.player_name or p.team_name,
                )
                continue
            if st == StorylineType.GOLDEN_BOOT:
                picked = _pick_goalscorer_leg(self._catalog, game, p.player_name)
            elif st == StorylineType.GOAL_MACHINES:
                picked = _pick_goalscorer_leg(self._catalog, game, p.player_name)
            elif st == StorylineType.RELEGATION:
                picked = _pick_relegation_leg(self._catalog, game, side)
            elif st == StorylineType.EUROPE_CHASE:
                picked = _pick_europe_chase_leg(self._catalog, game, side)
            elif st == StorylineType.TITLE_RACE:
                picked = _pick_title_race_leg(self._catalog, game, side)
            elif st == StorylineType.DERBY_WEEKEND:
                picked = _pick_derby_weekend_leg(self._catalog, game, side)
            elif st == StorylineType.EUROPEAN_WEEK:
                picked = _pick_european_week_leg(self._catalog, game, side)
            elif st == StorylineType.HOME_FORTRESS:
                picked = _pick_home_fortress_leg(self._catalog, game, side)
            else:  # unreachable — _SUPPORTED_TYPES gates at the top
                picked = None
            if picked is None:
                logger.info(
                    "CrossEventBuilder: no aligned leg for %s in fixture %s (type=%s) — skipping",
                    p.player_name or p.team_name, game.id, st.value,
                )
                continue
            legs.append(picked)
            seen_fixture_ids.add(game.id)
            resolved_participants.append(StorylineParticipant(
                player_name=p.player_name,
                team_name=p.team_name,
                fixture_id=game.id,
                extra=p.extra,
                # Carry the standings-verified context through so the
                # CombinedNarrativeAuthor can ground each team's framing
                # in real numbers. Additive field — empty dict on older
                # detector output.
                participant_context=getattr(p, "participant_context", {}) or {},
            ))

        if len(legs) < MIN_COMBO_LEGS:
            logger.info(
                "CrossEventBuilder: only %d legs resolved for storyline %s "
                "(type=%s, need >= %d) — skipping",
                len(legs), storyline.id, st.value, MIN_COMBO_LEGS,
            )
            return None

        selection_ids = [sel.selection_id for _, sel in legs if sel.selection_id]
        market_ids = [m.id for m, _ in legs]
        # Naive leg-product — caller (main._run_candidate_engine) will
        # re-price via calculate_bets combo path and overwrite this.
        try:
            product = 1.0
            for _, sel in legs:
                product *= float(sel.odds)
            naive_total = round(product, 2)
        except Exception:
            naive_total = None

        # Primary `game_id` — arbitrarily pick the first leg's fixture. The
        # field is required for CandidateCard but doesn't carry meaning for
        # cross-event combos; renderer uses `legs` + `selection_ids`.
        primary_game_id = legs[0][0].game_id if legs else ""

        # Mutate the storyline in place so `resolved_participants` (with
        # fixture_ids populated) is what the caller persists to the
        # storyline_items table. The original `storyline.participants` has
        # empty fixture_ids since the detector only knows player + team.
        storyline.participants = resolved_participants

        cand = CandidateCard(
            news_item_id=None,                       # no single news item owns this
            hook_type=HookType.OTHER,                # storyline-driven, not hook-driven
            bet_type=BetType.COMBO,
            game_id=primary_game_id,
            market_ids=market_ids,
            selection_ids=selection_ids,
            # Headline hint from detector becomes the narrative fallback.
            # `CombinedNarrativeAuthor` overwrites this with a synthesised
            # headline before publication.
            narrative=storyline.headline_hint or f"{st.value} storyline",
            status=CandidateStatus.DRAFT,
            total_odds=naive_total,
            price_source="naive",                    # overwritten by calculate_bets sweep
            virtual_selection=None,
            storyline_id=storyline.id,               # FK to storyline_items row
        )
        logger.info(
            "CrossEventBuilder: emitted COMBO candidate %s with %d legs "
            "(storyline=%s, fixtures=%s)",
            cand.id, len(legs), st.value,
            [game_id for game_id in seen_fixture_ids],
        )
        return cand
