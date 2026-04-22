"""Combo / Bet Builder engine — turns a single news item into a multi-leg pick.

Same-event Bet Builders on:
  - Main markets: FT 1X2, Total Goals O/U, BTTS, Double Chance, Draw No Bet.
  - Half markets: 1st Half 1X2.
  - Side markets: Corners FT O/U, Cards FT O/U.

Each theme picks 2-3 legs that hang together narratively. We keep BBs short
(≤3 legs) — Rogue rejects more correlations as the leg count climbs.

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
    # Injury to anyone on the affected side tends to dull the game. Back the
    # opponent (DNB is safer than 1X2 and strictly dominates it; don't stack
    # both), expect goals to dry up, fewer corners.
    HookType.INJURY: [
        ("draw_no_bet", "opponent"),
        ("over_under",  "under"),
        ("corners_ou",  "under"),
        ("btts",        "btts_no"),
    ],
    # Team news (return from suspension, fit XI, etc.) — lean into the
    # affected side attacking better.
    HookType.TEAM_NEWS: [
        ("match_result", "affected"),
        ("over_under",   "over"),
        ("corners_ou",   "over"),
        ("btts",         "btts_yes"),
    ],
    # Managerial quotes about "must-win" / "vital" tend to energise the
    # affected side. Lean to early intent (1st-half lead) + back them to win.
    HookType.MANAGER_QUOTE: [
        ("first_half_result", "affected"),
        ("match_result",      "affected"),
        ("over_under",        "over"),
    ],
    # Tactical stories (aggressive press, new formation, high block) — expect
    # set pieces and bookings, plus goals either way.
    HookType.TACTICAL: [
        ("corners_ou",   "over"),
        ("cards_ou",     "over"),
        ("over_under",   "over"),
        ("btts",         "btts_yes"),
    ],
    # Preview copy tends to point at the favourite. Use double-chance for
    # safety + Over 2.5 for the "lots of goals expected" angle.
    HookType.PREVIEW: [
        ("match_result", "affected"),
        ("over_under",   "over"),
    ],
    # Transfer — usually a new attacker. Back the affected side + goals.
    HookType.TRANSFER: [
        ("match_result", "affected"),
        ("over_under",   "over"),
    ],
    # Article / other -> no BB; the single-bet path handles these.
}

# Cap how many legs end up in any one BB. More legs => Rogue rejects more
# combos as uncorrelated. 3 is the sweet spot from prior PRs.
MAX_BB_LEGS = 3


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
) -> Optional[tuple[Market, MarketSelection]]:
    """Resolve (market_type, outcome_key) → (Market, MarketSelection) or None."""
    markets = [m for m in catalog.get_by_game(game.id) if m.market_type == market_type]
    if not markets:
        return None
    market = markets[0]
    selection = _find_selection(market, outcome_key, affected, game)
    if selection is None or not selection.selection_id:
        return None
    return market, selection


def _find_selection(
    market: Market,
    outcome_key: str,
    affected: Literal["home", "away"],
    game: Game,
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
    ):
        self._catalog = catalog
        self._rogue = rogue_client

    async def build(self, news: NewsItem, game: Game) -> Optional[CandidateCard]:
        """Build a Bet Builder candidate from a news item + its resolved fixture."""
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
            # we can fall through if the catalog is missing the preferred one;
            # we don't want both an over_under and a corners_ou + cards_ou
            # ballooning the BB to 4 legs in some unlucky case.
            if market_type in seen_market_types:
                continue
            picked = _pick_leg_selection(self._catalog, game, market_type, outcome_key, affected)
            if picked is None:
                continue
            legs.append(picked)
            seen_market_types.add(market_type)

        if len(legs) < 2:
            logger.debug("ComboBuilder: only %d valid legs for news %s", len(legs), news.id)
            return None

        selection_ids = [sel.selection_id for _, sel in legs if sel.selection_id]
        if len(selection_ids) != len(legs):
            return None

        # Validate via Rogue BB endpoint. Mock mode (no Rogue client) skips
        # validation and just trusts the combo — fine for local dev.
        total_odds: Optional[float] = None
        price_source: Optional[str] = None
        virtual_selection: Optional[str] = None
        if self._rogue is not None:
            try:
                resp = await self._rogue.betbuilder_match(selection_ids)
            except RogueApiError as exc:
                logger.info("ComboBuilder: Rogue rejected combo %s (%s)", selection_ids, exc)
                return None
            except Exception as exc:
                logger.warning("ComboBuilder: Rogue BB call errored: %s", exc)
                return None
            valid, odds, reason, virtual_selection = _parse_bb_validation(resp)
            if not valid:
                logger.info("ComboBuilder: combo invalid — %s (selection_ids=%s)", reason or "no reason", selection_ids)
                return None
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
