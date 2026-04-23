"""Cross-event builder — turns a StorylineItem into a multi-event CandidateCard.

Resolves each storyline participant to a real fixture in the current
catalogue, picks one aligned market leg per fixture (e.g. anytime-scorer
for a Golden Boot storyline), and emits a `CandidateCard` with
`bet_type=BetType.COMBO` and populated `selection_ids`. The post-engine
`calculate_bets` sweep in `main.py` stamps the real combo price.

Pricing validation (is this combo legal across events?) happens downstream
via `calculate_bets` returning a `Bets[Type='Combo']` entry. Unlike
same-event Bet Builders, cross-event combos don't need `betbuilder/match`
— the operator allows any cross-event permutation at the combined price.
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
) -> Optional[Game]:
    """Fuzzy match a team name (as the LLM wrote it) to a fixture in the catalogue.

    Match strategy: substring match against both home + away team `.name`
    (long form) and `.short_name` (abbreviated). First hit wins — the
    catalogue typically has <=20 fixtures in a single cycle so collisions
    are rare.
    """
    wanted = _lower_name(team_name)
    if not wanted:
        return None
    # Prefer exact match on full name first, then short name, then substring.
    for strategy in ("exact_name", "exact_short", "substring_name"):
        for g in games.values():
            home = _lower_name(g.home_team.name)
            away = _lower_name(g.away_team.name)
            home_short = _lower_name(g.home_team.short_name)
            away_short = _lower_name(g.away_team.short_name)
            if strategy == "exact_name" and wanted in (home, away):
                return g
            if strategy == "exact_short" and wanted in (home_short, away_short):
                return g
            if strategy == "substring_name" and (wanted in home or wanted in away
                                                  or home in wanted or away in wanted):
                return g
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


class CrossEventBuilder:
    """StorylineItem -> CandidateCard(COMBO).

    v1 supports `GOLDEN_BOOT` only. Other storyline types return None until
    a market-leg picker is wired per type (e.g. RELEGATION picks
    match_result=opponent for the at-risk team's fixture).
    """

    def __init__(self, catalog: MarketCatalog):
        self._catalog = catalog

    def build(
        self, storyline: StorylineItem, games: dict[str, Game]
    ) -> Optional[CandidateCard]:
        if storyline.storyline_type != StorylineType.GOLDEN_BOOT:
            logger.debug("CrossEventBuilder: type=%s not supported in v1",
                         storyline.storyline_type.value)
            return None

        legs: list[tuple[Market, MarketSelection]] = []
        resolved_participants: list[StorylineParticipant] = []
        seen_fixture_ids: set[str] = set()

        for p in storyline.participants:
            if len(legs) >= MAX_COMBO_LEGS:
                break
            if not p.player_name or not p.team_name:
                logger.debug("CrossEventBuilder: skipping participant with missing player/team: %s", p)
                continue
            game = _find_fixture_for_team(p.team_name, games)
            if game is None:
                logger.info(
                    "CrossEventBuilder: team %r not in catalogue — skipping participant %s",
                    p.team_name, p.player_name,
                )
                continue
            if game.id in seen_fixture_ids:
                # Two storyline participants playing in the same fixture —
                # we only want one leg per fixture so the combo stays
                # cross-event. Drop the second.
                logger.debug(
                    "CrossEventBuilder: duplicate fixture %s for storyline — skipping %s",
                    game.id, p.player_name,
                )
                continue
            picked = _pick_goalscorer_leg(self._catalog, game, p.player_name)
            if picked is None:
                logger.info(
                    "CrossEventBuilder: no goalscorer selection for %s in fixture %s — skipping",
                    p.player_name, game.id,
                )
                continue
            legs.append(picked)
            seen_fixture_ids.add(game.id)
            resolved_participants.append(StorylineParticipant(
                player_name=p.player_name,
                team_name=p.team_name,
                fixture_id=game.id,
                extra=p.extra,
            ))

        if len(legs) < MIN_COMBO_LEGS:
            logger.info(
                "CrossEventBuilder: only %d legs resolved for storyline %s "
                "(need >= %d) — skipping",
                len(legs), storyline.id, MIN_COMBO_LEGS,
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
            narrative=storyline.headline_hint or f"{storyline.storyline_type.value} storyline",
            status=CandidateStatus.DRAFT,
            total_odds=naive_total,
            price_source="naive",                    # overwritten by calculate_bets sweep
            virtual_selection=None,
            storyline_id=storyline.id,               # FK to storyline_items row
        )
        logger.info(
            "CrossEventBuilder: emitted COMBO candidate %s with %d legs "
            "(storyline=%s, fixtures=%s)",
            cand.id, len(legs), storyline.storyline_type.value,
            [game_id for game_id in seen_fixture_ids],
        )
        return cand
