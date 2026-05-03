"""Combination composer — turn a NarrativeThesis + market pool into a
ranked list of leg combinations.

Pure-function module. No HTTP, no DB, no LLM. Takes:

  * a `NarrativeThesis` (subject + archetype + resolved signals)
  * a list of available markets for a fixture (each enriched with
    `MarketMeta` via `market_meta.lookup_by_market_name`)

Returns ranked `Combination` objects. Each Combination is a tuple of
legs whose emitted signals overlap with the thesis signals and don't
contradict each other.

## Scoring

A combination's score is the sum of:

  * **+ signal_overlap_count** — count of distinct thesis signals
    matched across all legs (richer overlap → stronger story)
  * **+ archetype_affinity_total** — sum of per-leg affinity weights
    against the thesis archetype
  * **− conflict_penalty * 2** — direct signal contradictions
    (e.g. one leg `goals.high`, another `goals.low`) — hard penalty
  * **− orphan_leg_penalty** — legs with no signal overlap with the
    rest of the combo (filler that breaks coherence)

Subject-centric / discipline-only / subject_team_centric bet-shape
rules are HARD filters applied before scoring — combos that violate
them are rejected outright, not score-deducted.

## What this PR does NOT do

  * Resolve player_id from player name in the thesis subject — we
    string-match the subject_player_name against market `Selections`
    where it matters. A proper `entity_resolver` plug-in will land
    later; today's behaviour is "if the player name appears in a
    Goalscorer market's selection name, we accept the leg."
  * Talk to Rogue for combo legality — that stays with `combo_builder`,
    which will (next PR) consume the composer's output and price it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.engine import narrative_signals
from app.engine.market_meta import (
    CATALOGUE_BY_KEY,
    ENTITY_PLAYER,
    ENTITY_NAMED_TEAM,
    MarketMeta,
    lookup_by_market_name,
)
from app.engine.narrative_archetypes import (
    SUBJECT_MANAGER,
    SUBJECT_MATCH,
    SUBJECT_PLAYER,
    SUBJECT_TEAM,
)
from app.engine.narrative_thesis import NarrativeThesis


# ── Data classes ──────────────────────────────────────────────────────


BET_SHAPE_SINGLE = "single"
BET_SHAPE_BET_BUILDER = "bet_builder"


@dataclass(frozen=True)
class Leg:
    """One candidate leg in a combination.

    `is_bb_eligible` is set from the live Rogue `IsBetBuilderAvailable`
    flag at compose time — it's RUNTIME data per fixture per market,
    not a static metadata fact, because Rogue extends BB-eligibility
    over time per market type.
    """
    market_id: str           # Rogue market _id
    market_name: str
    market_meta_key: str     # MarketMeta.key — None-safe via empty string
    direction: str           # "over" / "under" / "yes" / "home" / etc.
    selection_id: Optional[str] = None
    selection_name: Optional[str] = None
    emitted_signals: tuple[str, ...] = ()
    archetype_affinity_weight: float = 0.0
    is_bb_eligible: bool = True


@dataclass(frozen=True)
class Combination:
    """A ranked combination of legs proposed by the composer.

    `bet_shape`:
      * `BET_SHAPE_BET_BUILDER` — multi-leg combo, every leg's
        selection is BB-eligible per Rogue's live
        `IsBetBuilderAvailable` flag → can be priced via
        `/v1/sportsdata/betbuilder/match` for correlated odds.
      * `BET_SHAPE_SINGLE` — single-leg "subject hero" card. Used when
        the composer can't fit a high-affinity subject leg into a BB
        (the leg isn't BB-eligible) but the leg has strong narrative
        value on its own. Priced as a single via the underlying
        market selection.
    """
    legs: tuple[Leg, ...]
    score: float
    signal_overlap_count: int
    archetype_affinity_total: float
    conflict_penalty: int
    orphan_legs: int
    rationale: str   # human-readable why-this-combo
    bet_shape: str = BET_SHAPE_BET_BUILDER


# ── Direction → emitted-signal resolution ─────────────────────────────


def _resolve_signals(meta: MarketMeta, direction: str,
                      *, home_team_id: Optional[str] = None,
                      away_team_id: Optional[str] = None,
                      named_team_id: Optional[str] = None,
                      opp_team_id: Optional[str] = None,
                      player_name: Optional[str] = None,
                      player_team_id: Optional[str] = None) -> tuple[str, ...]:
    """Resolve `{home}`, `{away}`, `{team}`, `{opp}`, `{p}`, `{p_team}`
    placeholders in the per-direction signal templates.
    """
    raw = meta.emits_signals_by_direction.get(direction, ())
    if not raw:
        # "any" fallback for markets where direction doesn't matter
        raw = meta.emits_signals_by_direction.get("any", ())
    out: list[str] = []
    for tmpl in raw:
        s = tmpl
        if "{home}" in s and home_team_id is not None:
            s = s.replace("{home}", str(home_team_id))
        if "{away}" in s and away_team_id is not None:
            s = s.replace("{away}", str(away_team_id))
        if "{team}" in s and named_team_id is not None:
            s = s.replace("{team}", str(named_team_id))
        if "{opp}" in s and opp_team_id is not None:
            s = s.replace("{opp}", str(opp_team_id))
        if "{p}" in s and player_name is not None:
            s = s.replace("{p}", str(player_name))
        if "{p_team}" in s and player_team_id is not None:
            s = s.replace("{p_team}", str(player_team_id))
        # Drop any leg that still has unresolved placeholders — they'd
        # never match and would skew counts.
        if "{" in s and "}" in s:
            continue
        out.append(s)
    return tuple(out)


# ── Bet-shape rule enforcement ────────────────────────────────────────


def _passes_bet_shape_rule(combo_legs: list[Leg],
                            thesis: NarrativeThesis) -> bool:
    """True iff combo_legs satisfy the archetype's bet_shape_rule."""
    if thesis.archetype is None:
        return True
    rule = thesis.archetype.bet_shape_rule
    if rule == "match_centric":
        return True
    if rule == "subject_centric":
        # Every leg must reference the subject (player or team)
        if thesis.subject_type == SUBJECT_PLAYER:
            sid = thesis.subject_player_name or thesis.subject_player_id
            if sid is None:
                return True  # composer will produce no candidates anyway
            for leg in combo_legs:
                if str(sid) not in (leg.selection_name or "") and \
                   not any(str(sid) in s for s in leg.emitted_signals):
                    return False
            return True
        if thesis.subject_type == SUBJECT_TEAM:
            tid = thesis.subject_team_id
            if tid is None:
                return True
            for leg in combo_legs:
                if not any(str(tid) in s for s in leg.emitted_signals):
                    return False
            return True
        return True
    if rule == "subject_team_centric":
        # Every leg must reference the subject's team OR the opp team
        # (relevant for KEY_*_OUT — bet flips around the opp team)
        tid = thesis.subject_team_id
        if tid is None:
            return True
        for leg in combo_legs:
            sigs_have_team = any(str(tid) in s for s in leg.emitted_signals)
            # Allow match-level legs that emit dominance.* / goals.* /
            # btts.* — these carry the thesis without naming the team.
            match_level_safe = any(
                s.startswith(("dominance.", "goals.", "btts.",
                              "clean_sheet.", "tempo.", "end_to_end"))
                for s in leg.emitted_signals
            )
            if not sigs_have_team and not match_level_safe:
                return False
        return True
    if rule == "discipline_only":
        # Every leg must be in {Cards, Player Specials, Discipline} groups —
        # we approximate via metadata key prefixes
        ok_keys = {
            "player_to_be_booked", "player_to_be_carded_first",
            "player_red_card", "cards_ft_ou", "cards_ft_1x2",
            "cards_first_half_ou", "team_total_cards_ou",
            "both_teams_to_be_booked", "total_match_fouls",
            "player_over_fouls", "player_over_tackles",
            "will_a_penalty_be_awarded",
        }
        return all(leg.market_meta_key in ok_keys for leg in combo_legs)
    return True


# ── Conflict detection ────────────────────────────────────────────────


def _conflict_count(legs: list[Leg]) -> int:
    """Count of distinct conflicting signal pairs across legs."""
    n = 0
    all_sigs = []
    for leg in legs:
        all_sigs.extend(leg.emitted_signals)
    seen = set()
    for i, a in enumerate(all_sigs):
        for b in all_sigs[i+1:]:
            key = tuple(sorted([a, b]))
            if key in seen:
                continue
            if narrative_signals.conflicts(a, b):
                seen.add(key)
                n += 1
    return n


# ── Orphan-leg detection ──────────────────────────────────────────────


def _orphan_count(legs: list[Leg]) -> int:
    """Legs whose emitted_signals share zero overlap with the rest."""
    if len(legs) < 2:
        return 0
    n = 0
    for i, leg in enumerate(legs):
        my = set(leg.emitted_signals)
        rest: set[str] = set()
        for j, other in enumerate(legs):
            if j == i:
                continue
            rest.update(other.emitted_signals)
        if not (my & rest):
            n += 1
    return n


# ── Candidate-leg construction from a market pool ─────────────────────


def _market_has_bb_eligible_in_direction(
    market: dict[str, Any],
    direction: str,
) -> bool:
    """For non-player markets: does the market have at least one
    BB-eligible selection that corresponds to `direction`?

    Conservative: when we can't tell which selection corresponds to
    which direction (e.g. correct score), require ANY selection to be
    BB-eligible. The composer's bet-shape filter is the safety net.
    """
    sels = market.get("Selections") or []
    for sel in sels:
        if sel.get("IsBetBuilderAvailable"):
            return True
    return False


def _decimal_odds(sel: dict[str, Any]) -> Optional[float]:
    """Pull decimal odds from a Rogue selection, defensively.

    Rogue's DisplayOdds is a dict like ``{"Decimal": "1.85", ...}``;
    Price may also exist as a numeric. Returns None when the price
    isn't readable.
    """
    do = sel.get("DisplayOdds")
    if isinstance(do, dict):
        v = do.get("Decimal")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    p = sel.get("Price")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def pick_line_for_player_selections(
    selections: list[dict[str, Any]],
    target_player_name: str,
    *,
    direction: str = "over",
    target_odds_band: tuple[float, float] = (1.7, 4.0),
) -> Optional[dict[str, Any]]:
    """Pick the best line for a player when a market lists multiple lines.

    Real example (MUN vs LIV): "Player Over Fouls" has 5 lines for
    Casemiro: Over 0.5 @ 1.18, Over 1.5 @ 1.84, Over 2.5 @ 3.49,
    Over 3.5 @ 8.00, Over 4.5 @ 19.00. The narratively-right pick
    is Over 1.5 (story = "he'll commit fouls", reads strong without
    being lottery-shaped). The composer needs to pick ONE.

    Heuristic v1: pick the selection whose decimal odds are closest
    to the middle of the `target_odds_band`. The default band
    `(1.7, 4.0)` corresponds to "narratively interesting without
    being a lock or a lottery." Tightens to `(1.5, 2.5)` when
    direction is "under" or for low-affinity legs (caller decides).

    Returns the selected selection dict, or `None` when no selection
    matches the target player.
    """
    if not selections or not target_player_name:
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    target_lc = target_player_name.lower()
    band_lo, band_hi = target_odds_band
    band_mid = (band_lo + band_hi) / 2.0
    for sel in selections:
        sname = (sel.get("Name") or sel.get("BetslipLine") or "").strip()
        if target_lc not in sname.lower():
            continue
        # If filtering by direction (e.g. "over"), match in the name
        if direction and direction.lower() not in sname.lower():
            # Some markets don't encode direction in selection name
            # (e.g. Anytime Goalscorer is yes-only). Skip the filter
            # then — no penalty.
            if direction == "yes":
                pass
            else:
                # Skip selections that don't match the requested direction
                # for OU-style markets where direction IS in the name.
                if "over" in sname.lower() or "under" in sname.lower():
                    continue
        odds = _decimal_odds(sel)
        if odds is None or odds <= 1.0:
            # Skip unprice-able / break-even
            continue
        # Distance from band midpoint, with bonus when inside the band.
        dist = abs(odds - band_mid)
        if band_lo <= odds <= band_hi:
            dist *= 0.5  # prefer in-band
        candidates.append((dist, sel))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def pick_line_for_match_market(
    selections: list[dict[str, Any]],
    *,
    direction: str = "over",
    target_odds_band: tuple[float, float] = (1.6, 2.5),
) -> Optional[dict[str, Any]]:
    """Pick the main line for a match-level OU/AH market when multiple
    lines exist (e.g. Cards FT O/U with one line at 4.5; FT O/U with
    lines at 0.5/1.5/2.5/3.5/4.5).

    Tighter band than player props because match-level lines should
    feel like the "book line" — close to even-money. Returns the
    selection in the requested direction whose odds are closest to
    the band midpoint.
    """
    if not selections:
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    band_lo, band_hi = target_odds_band
    band_mid = (band_lo + band_hi) / 2.0
    direction_lc = (direction or "").lower()
    for sel in selections:
        sname = (sel.get("Name") or "").strip().lower()
        if direction_lc and direction_lc not in sname:
            # Direction-encoded selection (Over/Under/Home/Away)
            continue
        odds = _decimal_odds(sel)
        if odds is None or odds <= 1.0:
            continue
        dist = abs(odds - band_mid)
        if band_lo <= odds <= band_hi:
            dist *= 0.5
        candidates.append((dist, sel))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _candidate_legs_for_market(
    market: dict[str, Any],
    meta: MarketMeta,
    thesis: NarrativeThesis,
    *,
    home_team_id: Optional[str] = None,
    away_team_id: Optional[str] = None,
) -> list[Leg]:
    """Build the candidate legs (one per relevant direction) for a single
    Rogue market under this thesis.

    Each leg carries `is_bb_eligible` derived from the live Rogue
    `IsBetBuilderAvailable` flag — composer's bet-shape filter uses
    this to keep BB combos shippable.
    """
    out: list[Leg] = []
    affinity = meta.archetype_affinities.get(
        thesis.archetype.key if thesis.archetype else "", (None, 0.0),
    )
    aff_dir, aff_weight = affinity

    # For player markets, find the SUBJECT player's selection and read
    # its individual BB-eligibility (player markets vary per-selection
    # — Player To Be Booked is 0/51 BB-eligible; Player Over Shots is
    # 190/190).
    selections = market.get("Selections") or []
    sel_id: Optional[str] = None
    sel_name: Optional[str] = None
    sel_is_bb_eligible: bool = False
    if meta.entity_scope == ENTITY_PLAYER and thesis.subject_player_name:
        # Player markets often list one selection per (player, line)
        # pair — e.g. Casemiro Over 0.5/1.5/2.5/3.5/4.5 fouls. Use the
        # line picker to choose narratively-right line.
        is_ou_player = (meta.claim_shape == "over_under")
        if is_ou_player:
            sel = pick_line_for_player_selections(
                selections, thesis.subject_player_name,
                direction="over",
            )
        else:
            # Yes/no player markets (Anytime Goalscorer, To Be Booked)
            # have one selection per player.
            sel = None
            for s in selections:
                sname = (s.get("Name") or s.get("BetslipLine") or "").strip()
                if thesis.subject_player_name.lower() in sname.lower():
                    sel = s
                    break
        if sel is not None:
            sel_id = sel.get("Id") or sel.get("_id")
            sel_name = (sel.get("Name") or sel.get("BetslipLine") or "").strip()
            sel_is_bb_eligible = bool(sel.get("IsBetBuilderAvailable"))
        if sel_id is None:
            return []  # subject player not present — drop this market
    else:
        # Non-player markets: BB-eligibility is per-direction in
        # principle, per-selection in practice. Conservative check.
        sel_is_bb_eligible = _market_has_bb_eligible_in_direction(
            market, direction="any",
        )

    for direction in meta.emits_signals_by_direction.keys():
        sigs = _resolve_signals(
            meta, direction,
            home_team_id=home_team_id, away_team_id=away_team_id,
            named_team_id=thesis.subject_team_id,
            opp_team_id=None,  # composer doesn't know opp yet for named_team markets
            player_name=thesis.subject_player_name,
            player_team_id=thesis.subject_team_id,
        )
        if not sigs:
            continue
        out.append(Leg(
            market_id=str(market.get("_id") or market.get("Id") or ""),
            market_name=market.get("Name") or market.get("MarketName") or "",
            market_meta_key=meta.key,
            direction=direction,
            selection_id=sel_id,
            selection_name=sel_name,
            emitted_signals=sigs,
            archetype_affinity_weight=(aff_weight if aff_dir else 0.0),
            is_bb_eligible=sel_is_bb_eligible,
        ))
    return out


# ── Main composer entry ───────────────────────────────────────────────


def compose_candidates(
    thesis: NarrativeThesis,
    market_pool: list[dict[str, Any]],
    *,
    home_team_id: Optional[str] = None,
    away_team_id: Optional[str] = None,
    target_legs: int = 3,
    min_legs: int = 2,
    max_combinations: int = 5,
    require_bb_eligibility: bool = True,
    emit_singles_for_subject_misses: bool = True,
) -> list[Combination]:
    """Score and rank candidate combinations.

    `require_bb_eligibility` (default `True`):
        For multi-leg combos, only legs whose underlying selection has
        Rogue's `IsBetBuilderAvailable=True` flag are eligible. Combos
        containing any non-BB-eligible leg are dropped (they couldn't
        be priced via `/v1/sportsdata/betbuilder/match`). Set `False`
        to compose without this constraint (e.g. for system bets or
        observability runs).

    `emit_singles_for_subject_misses` (default `True`):
        When the thesis is subject-centric (player) and a high-affinity
        subject-player leg ISN'T BB-eligible, emit it as a `single`
        Combination so the narrative still surfaces — just split into
        its own card downstream rather than getting silently dropped.

    Returns combinations of `bet_shape = bet_builder` (multi-leg, all
    BB-eligible) and `bet_shape = single` (1-leg, non-BB-eligible
    subject hero). Caller consumes both.
    """
    if thesis.archetype is None or thesis.is_uncertain or not market_pool:
        return []

    # 1. Resolve all candidate legs
    all_candidates: list[Leg] = []
    for market in market_pool:
        meta = lookup_by_market_name(
            market.get("Name") or market.get("MarketName") or ""
        )
        if meta is None:
            continue
        if meta.key in (thesis.archetype.forbidden_market_keys or ()):
            continue
        all_candidates.extend(_candidate_legs_for_market(
            market, meta, thesis,
            home_team_id=home_team_id, away_team_id=away_team_id,
        ))

    if not all_candidates:
        return []

    thesis_sigs = set(thesis.resolved_signals)

    # 2. Score legs in isolation (signal overlap + affinity)
    def leg_solo_score(leg: Leg) -> float:
        overlap = len(set(leg.emitted_signals) & thesis_sigs)
        return overlap * 1.0 + leg.archetype_affinity_weight * 1.5

    # ── 2a. Carve off non-BB-eligible high-affinity subject legs to be
    # singles candidates BEFORE BB-shape filtering. ──
    singles_combos: list[Combination] = []
    if (require_bb_eligibility and emit_singles_for_subject_misses
            and thesis.subject_type == SUBJECT_PLAYER
            and thesis.subject_player_name):
        # Pick the best 2 non-BB-eligible subject legs (cap to avoid
        # spamming the feed with one news item).
        subject_singles_pool = [
            l for l in all_candidates
            if not l.is_bb_eligible
            and l.selection_name
            and thesis.subject_player_name.lower() in l.selection_name.lower()
            and l.archetype_affinity_weight >= 0.7
        ]
        subject_singles_pool.sort(key=lambda l: -leg_solo_score(l))
        for l in subject_singles_pool[:2]:
            singles_combos.append(Combination(
                legs=(l,),
                score=round(leg_solo_score(l), 2),
                signal_overlap_count=len(set(l.emitted_signals) & thesis_sigs),
                archetype_affinity_total=round(l.archetype_affinity_weight, 2),
                conflict_penalty=0,
                orphan_legs=0,
                rationale=(
                    f"single — archetype={thesis.archetype.key} "
                    f"subject={thesis.subject_player_name} "
                    f"market={l.market_meta_key} "
                    f"(non-BB-eligible — surfaced as single card)"
                ),
                bet_shape=BET_SHAPE_SINGLE,
            ))

    # ── 2b. For BB combos, restrict to BB-eligible legs only. ──
    if require_bb_eligibility:
        bb_pool = [l for l in all_candidates if l.is_bb_eligible]
    else:
        bb_pool = list(all_candidates)
    scored = sorted(bb_pool, key=lambda l: -leg_solo_score(l))

    # 3. Greedy: build combos seeded from each top leg
    combos: list[Combination] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for seed in scored[:max_combinations * 3]:
        combo: list[Leg] = [seed]
        # Track which (market_meta_key, direction) pairs are taken to
        # avoid two legs from the same underlying market type.
        taken_keys = {seed.market_meta_key}
        while len(combo) < target_legs:
            best: Optional[tuple[Leg, float]] = None
            current_sigs = set()
            for leg in combo:
                current_sigs.update(leg.emitted_signals)
            for cand in scored:
                if cand.market_meta_key in taken_keys:
                    continue
                cand_sigs = set(cand.emitted_signals)
                marginal_overlap = len(cand_sigs & current_sigs)
                affinity_bonus = cand.archetype_affinity_weight * 1.5
                # Reject candidates that conflict
                if any(narrative_signals.conflicts(a, b)
                       for a in cand_sigs for b in current_sigs):
                    continue
                marginal_score = (
                    marginal_overlap * 1.0
                    + affinity_bonus
                    + len(cand_sigs & thesis_sigs) * 0.5
                )
                if best is None or marginal_score > best[1]:
                    best = (cand, marginal_score)
            if best is None or best[1] <= 0:
                break
            combo.append(best[0])
            taken_keys.add(best[0].market_meta_key)
        if len(combo) < min_legs:
            continue
        if not _passes_bet_shape_rule(combo, thesis):
            continue
        signature = tuple(sorted(l.market_meta_key + ":" + l.direction
                                  for l in combo))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        # Score the combo
        all_sigs: set[str] = set()
        for l in combo:
            all_sigs.update(l.emitted_signals)
        signal_overlap = len(all_sigs & thesis_sigs)
        affinity_total = sum(l.archetype_affinity_weight for l in combo)
        conflicts = _conflict_count(combo)
        orphans = _orphan_count(combo)
        score = (
            signal_overlap * 1.0
            + affinity_total * 1.5
            - conflicts * 2.0
            - orphans * 0.7
        )

        rationale = (
            f"bet_builder — archetype={thesis.archetype.key} "
            f"signals_matched={signal_overlap}/{len(thesis_sigs)} "
            f"affinity={affinity_total:.2f} "
            f"conflicts={conflicts} orphans={orphans} "
            f"bb_eligible={'all' if require_bb_eligibility else 'mixed'}"
        )
        combos.append(Combination(
            legs=tuple(combo),
            score=round(score, 2),
            signal_overlap_count=signal_overlap,
            archetype_affinity_total=round(affinity_total, 2),
            conflict_penalty=conflicts,
            orphan_legs=orphans,
            rationale=rationale,
            bet_shape=BET_SHAPE_BET_BUILDER,
        ))

    combos.sort(key=lambda c: -c.score)
    # Cap BB combos at max_combinations; singles ride alongside (capped at 2 above).
    return combos[:max_combinations] + singles_combos
