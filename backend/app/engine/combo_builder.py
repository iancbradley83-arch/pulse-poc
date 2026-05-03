"""Combo / Bet Builder engine — turns a single news item into a multi-leg pick.

Same-event Bet Builders on:
  - Main markets: FT 1X2, Total Goals O/U, BTTS, Double Chance, Draw No Bet.
  - Half markets: 1st Half 1X2.
  - Side markets: Corners FT O/U, Cards FT O/U.

Each theme picks 2-6 legs that hang together narratively. Leg count is
driven by narrative fit, not a fixed ceiling (PR #33 2026-04-23). When
Rogue rejects the combo we walk down one leg at a time until it
validates, down to the 2-leg minimum.

Pipeline:

  1. Hook type decides a *theme*: which side the news is pointing at, and what
     additional markets stack coherently with that signal.
  2. Look up each leg's selection in the MarketCatalog for the affected fixture.
  3. Ask Rogue `/v1/sportsdata/betbuilder/match` whether the combo is legal —
     some correlations the book rejects outright.
  4. If valid, emit a CandidateCard with `bet_type=BET_BUILDER` and all the
     leg selection_ids packed in. The rewriter + frontend handle display.

If *any* step fails (team can't be identified, a required market is missing
from the catalogue, Rogue rejects the combo), we return `None` and let the
single-bet candidate stand.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from app.engine.candidate_builder import (
    _affected_side_for_news,
    _dominant_out_position,
    _match_player_selection,
)
from app.models.news import BetType, CandidateCard, CandidateStatus, HookType, NewsItem
from app.models.schemas import Game, Market, MarketSelection
from app.services.market_catalog import MarketCatalog
from app.services.rogue_client import RogueApiError, RogueClient

logger = logging.getLogger(__name__)


# ── Hook theme rules ────────────────────────────────────────────────────────
#
# Each theme produces 2-3 legs. We encode them as abstract picks that are
# resolved against the affected side at build time:
#
#   (market_type, outcome_key)
#
# outcome_key is:
#   "affected"      -> the team the news is about wins
#   "opponent"      -> the other team wins
#   "over" / "under"-> O/U main line
#   "btts_yes" / "btts_no"
#   "dc_affected"   -> Double Chance favouring the affected side (1X or X2)

ThemeLeg = tuple[str, str]

# Default themes per hook. Kept conservative — two-leg BBs are less likely to
# be rejected by the book than 3-leg ones, and the voice rewriter can carry
# a lot of the storytelling load.
#
# After the market-coverage expansion, themes can pull from corners / cards /
# 1st-half / DNB markets too. Theme picker takes the FIRST 3 legs that
# successfully resolve (catalog has the market AND we can find the right
# selection); legs beyond that are dropped. So order matters — put the most
# narratively-aligned legs first.
HOOK_THEMES: dict[HookType, list[ThemeLeg]] = {
    # Injury THEME — position-aware routing (2026-04-23, re-enriched
    # 2026-04-24 for leg-count recovery). When the news carries
    # injury_details with a known position, _injury_theme_for() rewrites
    # this list into a position-specific stack. The entries below are the
    # *fallback* used when position is unknown. Enriched to 3 legs so the
    # typical unpositioned INJURY yields a 3-leg BB instead of the 2-leg
    # floor the PR #33 fix left behind. Order encodes narrative fit —
    # dedup-on-market-type + walk-down validator takes care of the rest.
    HookType.INJURY: [
        ("over_under", "over"),
        ("btts",       "btts_yes"),
        ("corners_ou", "over"),
    ],
    # Team news (return from suspension, fit XI, etc.) — lean into the
    # affected side attacking better. Goalscorer-by-mentioned-player is
    # tried first; if no player in the news matches the goalscorer market
    # it's skipped silently and the BB falls through to the generic legs.
    # Order favours: player-matched goalscorer → primary 1X2 → goals
    # thesis → BTTS alignment. 4 preferred slots so we frequently hit 4
    # when the full catalogue is available.
    HookType.TEAM_NEWS: [
        ("goalscorer",   "mentioned_player"),
        ("match_result", "affected"),
        ("over_under",   "over"),
        ("btts",         "btts_yes"),
        ("corners_ou",   "over"),
    ],
    # Tactical stories (aggressive press, new formation, high block) —
    # expect set pieces and bookings, plus goals either way. Enriched to
    # 4 preferred legs so a TACTICAL BB reads like a tactical BB instead
    # of a generic over/btts.
    HookType.TACTICAL: [
        ("corners_ou",   "over"),
        ("cards_ou",     "over"),
        ("over_under",   "over"),
        ("btts",         "btts_yes"),
    ],
    # Preview copy tends to point at the favourite. Now that PREVIEW is
    # "both" in the per-hook preference (2026-04-24), we want the BB to
    # carry its own weight — 3 legs: primary pick + goals thesis + BTTS.
    HookType.PREVIEW: [
        ("match_result", "affected"),
        ("over_under",   "over"),
        ("btts",         "btts_yes"),
    ],
    # Transfer — usually a new attacker. Lead with the named player as
    # anytime scorer if we can match them; back the affected side + goals.
    HookType.TRANSFER: [
        ("goalscorer",   "mentioned_player"),
        ("match_result", "affected"),
        ("over_under",   "over"),
        ("btts",         "btts_yes"),
    ],
    # Manager quote that names a specific player (e.g. "I expect Saka to
    # punish them today") — reuse the player-aware path. 1st-half angle
    # fits a pre-match quote thesis ("we want to start on the front foot")
    # and stacks cleanly with FT outcome + over.
    HookType.MANAGER_QUOTE: [
        ("goalscorer",           "mentioned_player"),
        ("first_half_result",    "affected"),
        ("first_half_goals_ou",  "over"),
        ("match_result",         "affected"),
        ("over_under",           "over"),
    ],
    # Article / other -> no BB; the single-bet path handles these.
}

# Alternate legs per hook theme for the leg-swap retry path (2026-04-24).
# When Rogue rejects the initial N-leg combo, instead of immediately
# dropping the last leg, we try swapping in one of these alternates for
# the last leg at the same leg-count. Only after swap attempts exhaust do
# we drop to N-1. Each alternate is a (market_type, outcome_key) pair.
#
# Capped at 2 alternates per hook — keeps the total validator-call budget
# sane (see _MAX_VALIDATOR_CALLS below). Order matters: first alternate
# is tried before second.
HOOK_ALTERNATES: dict[HookType, list[ThemeLeg]] = {
    HookType.INJURY: [
        ("double_chance", "dc_affected"),
        ("cards_ou",      "over"),
    ],
    HookType.TEAM_NEWS: [
        ("double_chance", "dc_affected"),
        ("cards_ou",      "over"),
    ],
    HookType.TACTICAL: [
        ("double_chance", "dc_affected"),
        ("first_half_goals_ou", "over"),
    ],
    HookType.PREVIEW: [
        ("double_chance", "dc_affected"),
        ("corners_ou",    "over"),
    ],
    HookType.TRANSFER: [
        ("corners_ou",    "over"),
        ("double_chance", "dc_affected"),
    ],
    HookType.MANAGER_QUOTE: [
        ("btts",          "btts_yes"),
        ("corners_ou",    "over"),
    ],
}

# Budget cap so a pathological rejection cycle can't blow our Rogue
# call count for a single BB build. Walk-down from N legs (≈ 4) through
# one alternate-leg swap each is ≤ 5 calls; leave headroom.
_MAX_VALIDATOR_CALLS = 5

# BB leg count is driven by narrative fit, not a fixed count (user decision
# 2026-04-23, PR #33 review feedback point 4). We accept 2..6 legs; the
# Rogue betbuilder/match endpoint is the ground truth for whether the combo
# is legal. When more than MAX_BB_LEGS candidate legs resolve, the dedup-
# on-market-type rule + theme ordering (most narratively-aligned first,
# player-matched legs preferred) picks the top MAX_BB_LEGS.
MIN_BB_LEGS = 2
MAX_BB_LEGS = 6


# Position-aware INJURY theme. Mirrors _INJURY_ROUTES in candidate_builder
# but expressed as ComboBuilder ThemeLegs so we reuse the existing leg
# resolver. Each list has at least 3 candidates so the MAX_BB_LEGS cap
# leaves room to fall through if one market is missing.
_INJURY_POSITION_THEMES: dict[str, list[ThemeLeg]] = {
    # Attacker out on affected side => game dries up. 3-leg stack: under
    # goals + BTTS no + cards-under. Fallback tail (corners_under) gives
    # the resolver somewhere to go when cards market isn't in the
    # catalogue; dedup-on-market-type keeps only one.
    "striker": [
        ("over_under", "under"),
        ("btts",       "btts_no"),
        ("cards_ou",   "under"),
        ("corners_ou", "under"),
    ],
    "winger": [
        ("over_under", "under"),
        ("btts",       "btts_no"),
        ("cards_ou",   "under"),
        ("corners_ou", "under"),
    ],
    "attacking_mid": [
        ("over_under", "under"),
        ("btts",       "btts_no"),
        ("cards_ou",   "under"),
        ("corners_ou", "under"),
    ],
    # Defender out (CB / FB) => goals open both ways, corners and cards
    # both up. 4-leg stack: overs + BTTS yes + corners over + cards over.
    "centre_back": [
        ("over_under", "over"),
        ("btts",       "btts_yes"),
        ("corners_ou", "over"),
        ("cards_ou",   "over"),
    ],
    "fullback": [
        ("over_under", "over"),
        ("btts",       "btts_yes"),
        ("corners_ou", "over"),
        ("cards_ou",   "over"),
    ],
    # GK out → specific variant: wider spread of direct goal-threat legs.
    "goalkeeper": [
        ("over_under", "over"),
        ("btts",       "btts_yes"),
        ("corners_ou", "over"),
        ("cards_ou",   "over"),
    ],
    # Defensive mid => chaos signals; game opens up (3-leg).
    "defensive_mid": [
        ("corners_ou", "over"),
        ("cards_ou",   "over"),
        ("over_under", "over"),
    ],
}


def _injury_theme_for(news: NewsItem, game: Game) -> Optional[list[ThemeLeg]]:
    """If `news` is an INJURY item with actionable position data, return a
    position-aware theme list. Else None — caller uses HOOK_THEMES[INJURY]."""
    if news.hook_type != HookType.INJURY:
        return None
    side = _affected_side_for_news(news, game)
    if side is None:
        return None
    affected_team_name = (
        game.home_team.name if side == "home" else game.away_team.name
    )
    pos = _dominant_out_position(news.injury_details, affected_team_name)
    if pos is None:
        return None
    theme = _INJURY_POSITION_THEMES.get(pos)
    if theme:
        logger.info(
            "ComboBuilder: INJURY theme position=%s (game=%s, news=%s)",
            pos, game.id, news.id,
        )
    return theme


def _affected_side(
    news: NewsItem, game: Game
) -> Optional[Literal["home", "away"]]:
    """Which side of the fixture is the story about? None if we can't tell."""
    home = game.home_team.id in news.team_ids
    away = game.away_team.id in news.team_ids
    if home and not away:
        return "home"
    if away and not home:
        return "away"
    if home and away:
        # Story names both — default to the home side as "affected" (hosts
        # are the more natural anchor for most narratives).
        return "home"
    return None


def _pick_leg_selection(
    catalog: MarketCatalog,
    game: Game,
    market_type: str,
    outcome_key: str,
    affected: Literal["home", "away"],
    mentions: Optional[list[str]] = None,
) -> Optional[tuple[Market, MarketSelection]]:
    """Resolve (market_type, outcome_key) → (Market, MarketSelection) or None."""
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == market_type]
    if not markets:
        return None
    market = markets[0]
    selection = _find_selection(market, outcome_key, affected, game, mentions)
    if selection is None or not selection.selection_id:
        return None
    return market, selection


def _find_selection(
    market: Market,
    outcome_key: str,
    affected: Literal["home", "away"],
    game: Game,
    mentions: Optional[list[str]] = None,
) -> Optional[MarketSelection]:
    """Pick the right selection from a market based on the abstract outcome key."""
    opponent = "away" if affected == "home" else "home"

    def by_outcome(o: str) -> Optional[MarketSelection]:
        wanted = o.lower()
        for sel in market.selections:
            if (sel.outcome_type or "").lower() == wanted:
                return sel
        return None

    def by_label_substr(*needles: str) -> Optional[MarketSelection]:
        for sel in market.selections:
            label = (sel.label or "").lower()
            if all(n in label for n in needles):
                return sel
        return None

    if outcome_key == "affected":
        return by_outcome(affected)
    if outcome_key == "opponent":
        return by_outcome(opponent)
    if outcome_key == "over":
        return by_outcome("over") or by_label_substr("over")
    if outcome_key == "under":
        return by_outcome("under") or by_label_substr("under")
    if outcome_key == "btts_yes":
        return by_label_substr("yes") or (market.selections[0] if market.selections else None)
    if outcome_key == "btts_no":
        return by_label_substr("no") or (market.selections[1] if len(market.selections) > 1 else None)
    if outcome_key == "dc_affected":
        # Double chance favouring the affected side. Rogue labels are
        # typically "1X" / "12" / "X2"; for home-affected we want 1X
        # (home or draw), for away-affected we want X2 (draw or away).
        wanted = "1x" if affected == "home" else "x2"
        sel = by_label_substr(wanted)
        if sel is not None:
            return sel
        # Some books use "Home or Draw" / "Draw or Away" labels — try a
        # word-based fallback before giving up.
        if affected == "home":
            return by_label_substr("home", "draw") or by_label_substr("home or draw")
        return by_label_substr("draw", "away") or by_label_substr("draw or away")
    if outcome_key == "mentioned_player":
        # Goalscorer-leg-by-name. Returns None if no mentioned player matches
        # a selection in this market — caller falls through to the next leg.
        if not mentions:
            return None
        return _match_player_selection(market, mentions)
    return None


def _parse_bb_validation(resp: Any) -> tuple[bool, Optional[float], str, Optional[str]]:
    """Normalise Rogue BB response into (valid, total_odds, reason, virtual_selection_id).

    Rogue's `/v1/sportsdata/betbuilder/match` response shape (verified against
    prod 2026-04-21):

        { "IsSuccess": bool,
          "AvailableSelectionIds": [...more selections you could add...],
          "VirtualSelection": "0VS<piped-ids>" }

    No combined odds in the response — callers must compute their own (or quote
    via Kmianko bet-slip using the returned `VirtualSelection` id, which is
    what `ComboBuilder.build` does when a `KmiankoBetslipClient` is provided).
    """
    if resp is None:
        return False, None, "no response", None
    if not isinstance(resp, dict):
        return False, None, f"unexpected response shape: {type(resp).__name__}", None

    # Canonical Rogue response key
    if "IsSuccess" in resp:
        valid = bool(resp["IsSuccess"])
    else:
        # Legacy / fallback keys — keep these so the parser still works if
        # Rogue changes the shape.
        valid = None
        for key in ("IsValid", "Valid", "isValid", "valid"):
            if key in resp:
                valid = bool(resp[key])
                break
        if valid is None:
            # If a VirtualSelection came back, treat as valid (the book
            # accepted the combo enough to give it an ID).
            valid = bool(resp.get("VirtualSelection"))

    # Total odds are not in the response — caller multiplies leg decimals.
    odds = None
    for key in ("TotalOdds", "CombinedOdds", "totalOdds", "combinedOdds"):
        v = resp.get(key)
        if isinstance(v, (int, float, str)) and v != "":
            try:
                odds = float(v)
                break
            except (TypeError, ValueError):
                pass
    if odds is None:
        disp = resp.get("DisplayOdds") or resp.get("displayOdds")
        if isinstance(disp, dict):
            try:
                dv = disp.get("Decimal") or 0
                odds = float(dv) or None
            except (TypeError, ValueError):
                odds = None

    reason = ""
    for key in ("Reason", "Message", "Error", "errorMessage"):
        v = resp.get(key)
        if isinstance(v, str) and v:
            reason = v
            break

    virtual_selection = resp.get("VirtualSelection")
    if not isinstance(virtual_selection, str) or not virtual_selection:
        virtual_selection = None

    return bool(valid), odds, reason, virtual_selection


class ComboBuilder:
    def __init__(
        self,
        catalog: MarketCatalog,
        rogue_client: Optional[RogueClient],
        *,
        event_payload_by_game_id: Optional[dict[str, dict[str, Any]]] = None,
        narrative_telemetry: Optional[Any] = None,
    ):
        self._catalog = catalog
        self._rogue = rogue_client
        # Per-fixture raw event payload (Rogue includeMarkets="all"
        # response) keyed by game.id. When supplied, `build_narrative()`
        # uses it as the market pool for the composer; without it the
        # composer falls back to the catalog's normalized markets which
        # don't carry per-selection IsBetBuilderAvailable.
        self._event_payload_by_game_id = event_payload_by_game_id or {}
        self._narrative_telemetry = narrative_telemetry

    async def build_narrative(
        self,
        news: NewsItem,
        game: Game,
        *,
        publish: bool = False,
    ) -> list[CandidateCard]:
        """Phase 3.5 narrative composer path — runs alongside `build()`.

        When `PULSE_NARRATIVE_COMPOSER_ENABLED=true` and the news matches
        an archetype, the composer builds a thesis from the news, runs
        `compose_candidates()` against the per-fixture market pool, and
        produces:
          * 1 BB-shape CandidateCard from the top-scoring BB-eligible
            combination, OR no BB if the archetype produced none
          * Up to 2 single-shape CandidateCards for high-affinity
            subject-player legs that aren't BB-eligible

        Telemetry is captured to `narrative_telemetry` regardless of
        `publish`. When `publish=False` (default — shadow mode) the
        method returns an empty list and the caller publishes nothing
        from the composer; the captured telemetry still flows.

        Returns the list of CandidateCards (empty if nothing matches,
        nothing publishable, or `publish=False`).
        """
        # Lazy imports to keep the optional path off the import graph
        # for callers that never enable the composer.
        from app.engine.combination_composer import (
            BET_SHAPE_BET_BUILDER,
            BET_SHAPE_SINGLE,
            compose_candidates,
        )
        from app.engine.narrative_thesis import build_thesis

        thesis = build_thesis(news)
        if thesis.is_uncertain or thesis.archetype is None:
            logger.info(
                "[narrative_uncertain] news=%s confidence=%.2f "
                "matched_keywords=%s — no archetype, no composer output",
                news.id, thesis.confidence,
                list(thesis.matched_keywords),
            )
            return []

        # Telemetry — capture every thesis decision, even before any
        # combo decision. Thesis IDs link compositions back.
        thesis_id: Optional[int] = None
        if self._narrative_telemetry is not None:
            thesis_id = await self._narrative_telemetry.save_thesis(thesis)

        # Pull the per-fixture event payload (composer's market pool).
        # Without it the composer can't reason over BB-eligibility — log
        # and return so it's visible.
        event_payload = self._event_payload_by_game_id.get(game.id)
        if not event_payload:
            logger.info(
                "[narrative_composer] news=%s game=%s — no event_payload "
                "for fixture; composer would need raw market data with "
                "IsBetBuilderAvailable. Skipping.",
                news.id, game.id,
            )
            return []

        market_pool = event_payload.get("Markets") or []
        # Resolve home / away ids for signal placeholder filling.
        participants = event_payload.get("Participants") or []
        home_id = (participants[0] or {}).get("_id") if participants else None
        away_id = (participants[1] or {}).get("_id") if len(participants) > 1 else None

        combos = compose_candidates(
            thesis, market_pool,
            home_team_id=home_id, away_team_id=away_id,
            target_legs=4, min_legs=2,
            require_bb_eligibility=True,
            emit_singles_for_subject_misses=True,
        )

        # Telemetry: persist every composition (BB + singles), regardless
        # of publish flag, so we can compare shadow runs to live runs.
        if combos and self._narrative_telemetry is not None and thesis_id is not None:
            for combo in combos:
                await self._narrative_telemetry.save_composition(
                    thesis_id=thesis_id,
                    candidate_card_id=None,  # set when publishing path lands
                    combination=combo,
                )

        # Log composition summary
        bb_count = sum(1 for c in combos if c.bet_shape == BET_SHAPE_BET_BUILDER)
        single_count = sum(1 for c in combos if c.bet_shape == BET_SHAPE_SINGLE)
        logger.info(
            "[narrative_composer] news=%s game=%s archetype=%s "
            "subject=%s combos=%d (bb=%d singles=%d) publish=%s",
            news.id, game.id, thesis.archetype.key,
            thesis.subject_player_name or thesis.subject_team_id or "match",
            len(combos), bb_count, single_count, publish,
        )

        if not publish:
            return []

        # PUBLISH path — convert Combinations to CandidateCards.
        # Today's shape: emit one CandidateCard per Combination, tagged
        # with bet_type=BET_BUILDER for bb_shape, BET_TYPE_SINGLE for
        # singles. selection_ids drive deep-link minting downstream.
        cards: list[CandidateCard] = []
        for combo in combos:
            sel_ids = [
                l.selection_id for l in combo.legs
                if l.selection_id
            ]
            if not sel_ids:
                # Without selection_ids we can't deep-link; skip.
                continue
            bet_type = (
                BetType.BET_BUILDER
                if combo.bet_shape == BET_SHAPE_BET_BUILDER and len(combo.legs) > 1
                else BetType.SINGLE
            )
            card = CandidateCard(
                news_item_id=news.id,
                hook_type=news.hook_type,
                bet_type=bet_type,
                game_id=game.id,
                selection_ids=sel_ids,
                market_ids=[l.market_id for l in combo.legs],
                status=CandidateStatus.DRAFT,
                reason=combo.rationale,
            )
            cards.append(card)
        return cards

    async def build(self, news: NewsItem, game: Game) -> Optional[CandidateCard]:
        """Build a Bet Builder candidate from a news item + its resolved fixture."""
        # Position-aware INJURY theme overrides HOOK_THEMES[INJURY] when the
        # news carries structured injury_details (2026-04-23). Falls through
        # to the generic INJURY theme otherwise.
        position_theme = _injury_theme_for(news, game)
        if position_theme is not None:
            theme = position_theme
        else:
            theme = HOOK_THEMES.get(news.hook_type)
        if theme is None:
            return None
        affected = _affected_side(news, game)
        if affected is None:
            logger.debug("ComboBuilder: no affected side for news %s (game %s)", news.id, game.id)
            return None

        legs: list[tuple[Market, MarketSelection]] = []
        seen_market_types: set[str] = set()
        for market_type, outcome_key in theme:
            if len(legs) >= MAX_BB_LEGS:
                break
            # One leg per market_type — themes list multiple alternates so
            # we can fall through if the catalog is missing the preferred
            # one. Dedup-on-market-type preserved even under the 6-leg cap
            # (PR #33 feedback point 4): order in the theme encodes
            # narrative fit / player-matched preference, and we trust the
            # author to put the must-have legs first.
            if market_type in seen_market_types:
                continue
            picked = _pick_leg_selection(
                self._catalog, game, market_type, outcome_key, affected,
                mentions=news.mentions,
            )
            if picked is None:
                continue
            legs.append(picked)
            seen_market_types.add(market_type)

        if len(legs) < MIN_BB_LEGS:
            logger.debug("ComboBuilder: only %d valid legs for news %s", len(legs), news.id)
            return None

        selection_ids = [sel.selection_id for _, sel in legs if sel.selection_id]
        if len(selection_ids) != len(legs):
            return None

        # Pre-resolve alternate legs for the leg-swap retry path. Each
        # alternate is tried BEFORE dropping a leg, so we frequently land
        # on 3-leg BBs that would otherwise collapse to the 2-leg floor
        # just because one specific leg correlates poorly. Alternates are
        # filtered to exclude market_types already in the primary stack —
        # swapping e.g. over_under for corners_ou when corners_ou is
        # already leg 3 would just duplicate.
        alternates_raw = HOOK_ALTERNATES.get(news.hook_type, [])
        alternate_legs: list[tuple[Market, MarketSelection]] = []
        seen_alt_types: set[str] = set(seen_market_types)
        for market_type, outcome_key in alternates_raw:
            if market_type in seen_alt_types:
                continue
            picked = _pick_leg_selection(
                self._catalog, game, market_type, outcome_key, affected,
                mentions=news.mentions,
            )
            if picked is None:
                continue
            alternate_legs.append(picked)
            seen_alt_types.add(market_type)

        # Validate via Rogue BB endpoint. Mock mode (no Rogue client) skips
        # validation and just trusts the combo — fine for local dev.
        #
        # Walk order (2026-04-24 leg-swap retry, PR "fix/leg-counts-mix-balance"):
        #   1. Full N-leg stack.
        #   2. Same-leg-count swaps: for each resolved alternate, swap it
        #      into the LAST leg slot (the least-narratively-critical
        #      position). Caps at len(alternate_legs) tries.
        #   3. Drop to N-1, N-2, … MIN_BB_LEGS.
        # Total validator calls capped at _MAX_VALIDATOR_CALLS so a
        # pathological rejection cycle can't blow our Rogue budget for a
        # single BB build. Previous behaviour (PR #33) went straight to
        # drop-legs which bottomed out at 2 legs most of the time.
        total_odds: Optional[float] = None
        price_source: Optional[str] = None
        virtual_selection: Optional[str] = None
        if self._rogue is not None:
            valid = False
            reason = ""
            odds = None
            n = len(selection_ids)

            # Build ordered attempt list. Each entry is (legs_list,
            # selection_ids_list, label) so logging can explain which
            # variant was accepted.
            attempts: list[tuple[
                list[tuple[Market, MarketSelection]], list[str], str,
            ]] = []
            # 1. Full primary.
            attempts.append((list(legs), list(selection_ids), f"primary-{n}leg"))
            # 2. Same-leg-count alternates: replace last leg with each alt.
            for i, alt in enumerate(alternate_legs):
                if len(attempts) >= _MAX_VALIDATOR_CALLS:
                    break
                swapped_legs = legs[:-1] + [alt]
                swapped_ids = [s.selection_id for _, s in swapped_legs]
                if None in swapped_ids or len(swapped_ids) != len(swapped_legs):
                    continue
                attempts.append((swapped_legs, swapped_ids, f"swap{i}-{n}leg"))
            # 3. Drop-leg walk (N-1 down to MIN_BB_LEGS).
            for size in range(n - 1, MIN_BB_LEGS - 1, -1):
                if len(attempts) >= _MAX_VALIDATOR_CALLS:
                    break
                attempts.append((legs[:size], selection_ids[:size], f"drop-{size}leg"))

            accepted: Optional[tuple[
                list[tuple[Market, MarketSelection]], list[str], str,
            ]] = None
            for attempt_legs, attempt_ids, label in attempts:
                try:
                    resp = await self._rogue.betbuilder_match(attempt_ids)
                except RogueApiError as exc:
                    logger.info(
                        "ComboBuilder: Rogue rejected combo %s (%s, attempt=%s)",
                        attempt_ids, exc, label,
                    )
                    return None
                except Exception as exc:
                    logger.warning("ComboBuilder: Rogue BB call errored: %s", exc)
                    return None
                valid, odds, reason, virtual_selection = _parse_bb_validation(resp)
                if valid:
                    accepted = (attempt_legs, attempt_ids, label)
                    break
                logger.info(
                    "ComboBuilder: combo invalid — %s (attempt=%s, selection_ids=%s)",
                    reason or "no reason", label, attempt_ids,
                )

            if accepted is None:
                return None
            accepted_legs, accepted_ids, accepted_label = accepted
            if accepted_ids != selection_ids:
                logger.info(
                    "ComboBuilder: %d-leg primary not accepted, used %s (%d leg, news=%s)",
                    n, accepted_label, len(accepted_ids), news.id,
                )
            legs = accepted_legs
            selection_ids = accepted_ids
            # Rogue itself returns no correlated odds. If we have a Kmianko
            # client and a VirtualSelection id, use those to get the *real*
            # operator price.
            if odds:
                total_odds = odds
                price_source = "rogue_bb"

        # Real correlated BB price via Rogue's official Betting API
        # (POST /v1/betting/calculateBets). Same anonymous Bearer JWT, same
        # host as the rest of RogueClient — no separate auth, no headless
        # browser. Returns per-leg odds + a Bets[] array of supportable bet
        # types; for a BB virtual-selection id the relevant entry is
        # Type=='BetBuilder' carrying the correlated TrueOdds.
        if total_odds is None and self._rogue is not None and virtual_selection:
            try:
                quote = await self._rogue.calculate_bets([virtual_selection])
            except Exception as exc:
                logger.warning("ComboBuilder: calculate_bets errored for %s — %s",
                               virtual_selection[:48], exc)
                quote = None
            if isinstance(quote, dict):
                bets = quote.get("Bets") or []
                bet_types = [(b or {}).get("Type") for b in bets]
                bb_bet = next(
                    (b for b in bets if (b or {}).get("Type") in ("BetBuilder", "Single")),
                    None,
                )
                if bb_bet and isinstance(bb_bet.get("TrueOdds"), (int, float)):
                    total_odds = round(float(bb_bet["TrueOdds"]), 2)
                    price_source = "rogue_calculate_bets"
                    logger.info(
                        "ComboBuilder: BB priced at %.2f via calculate_bets (vs=%s, bet_types=%s)",
                        total_odds, virtual_selection[:48], bet_types,
                    )
                else:
                    # API returned but didn't give us a BB total — log the
                    # whole shape so we can see what we're missing.
                    errors = quote.get("Errors") or []
                    nonactive = quote.get("NonActiveSelections") or {}
                    logger.warning(
                        "ComboBuilder: calculate_bets returned no usable BB price "
                        "(vs=%s, bet_types=%s, errors=%s, nonactive=%s)",
                        virtual_selection[:48], bet_types,
                        [e.get("Error") for e in errors[:3]],
                        {k: v for k, v in nonactive.items() if v},
                    )
            elif quote is None:
                logger.warning(
                    "ComboBuilder: calculate_bets returned None for %s",
                    virtual_selection[:48],
                )

        # Fallback total: multiply decimal odds naively if neither Rogue nor
        # Kmianko gave us a real number. Naive product over-states correlated
        # BBs by ~1-2x, so this is *only* useful for the quality gate — the
        # frontend hides total_odds when price_source == 'naive'.
        if total_odds is None:
            try:
                product = 1.0
                for _, sel in legs:
                    product *= float(sel.odds)
                total_odds = round(product, 2)
                price_source = "naive"
            except Exception:
                total_odds = None
                price_source = None

        market_ids = [m.id for m, _ in legs]
        narrative = news.headline or news.summary or ""

        cand = CandidateCard(
            news_item_id=news.id,
            hook_type=news.hook_type,
            bet_type=BetType.BET_BUILDER,
            game_id=game.id,
            market_ids=market_ids,
            selection_ids=selection_ids,
            narrative=narrative,
            status=CandidateStatus.DRAFT,
            virtual_selection=virtual_selection,
            total_odds=total_odds,
            price_source=price_source,
        )
        return cand
