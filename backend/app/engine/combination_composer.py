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


@dataclass(frozen=True)
class Leg:
    """One candidate leg in a combination."""
    market_id: str           # Rogue market _id
    market_name: str
    market_meta_key: str     # MarketMeta.key — None-safe via empty string
    direction: str           # "over" / "under" / "yes" / "home" / etc.
    selection_id: Optional[str] = None
    selection_name: Optional[str] = None
    emitted_signals: tuple[str, ...] = ()
    archetype_affinity_weight: float = 0.0


@dataclass(frozen=True)
class Combination:
    """A ranked combination of legs proposed by the composer."""
    legs: tuple[Leg, ...]
    score: float
    signal_overlap_count: int
    archetype_affinity_total: float
    conflict_penalty: int
    orphan_legs: int
    rationale: str   # human-readable why-this-combo


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


def _candidate_legs_for_market(
    market: dict[str, Any],
    meta: MarketMeta,
    thesis: NarrativeThesis,
    *,
    home_team_id: Optional[str] = None,
    away_team_id: Optional[str] = None,
) -> list[Leg]:
    """Build the candidate legs (one per relevant direction) for a single
    Rogue market under this thesis."""
    out: list[Leg] = []
    affinity = meta.archetype_affinities.get(
        thesis.archetype.key if thesis.archetype else "", (None, 0.0),
    )
    aff_dir, aff_weight = affinity

    # For player markets, the SUBJECT must be the player (subject_centric)
    # OR for opp-archetypes, the player must NOT be the subject (no
    # composer support yet — we just pull the first BB-eligible
    # selection that matches the subject_player_name).
    selections = market.get("Selections") or []
    sel_id: Optional[str] = None
    sel_name: Optional[str] = None
    if meta.entity_scope == ENTITY_PLAYER and thesis.subject_player_name:
        # Try to find the subject player among selections
        for sel in selections:
            sname = (sel.get("Name") or sel.get("BetslipLine") or "").strip()
            if thesis.subject_player_name.lower() in sname.lower():
                sel_id = sel.get("Id") or sel.get("_id")
                sel_name = sname
                break
        if sel_id is None:
            return []  # subject player not present — drop this market

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
) -> list[Combination]:
    """Score and rank candidate combinations.

    Returns up to `max_combinations`. Each combination has between
    `min_legs` and `target_legs` legs. The composer also yields
    single-leg "subject hero" combinations when the archetype is
    subject-centric (these become singles cards downstream).

    Today's heuristic walk:
      1. Build all candidate legs from the market pool that pass
         per-leg metadata + thesis filters.
      2. Score each leg by signals + affinity in isolation; keep top-N
         per market_meta key.
      3. Greedy-extend combos by adding the leg with the largest
         marginal signal overlap, until target_legs reached or no
         improving leg remains.
      4. Apply bet-shape rule + conflict count + orphan count.
      5. Return top-K by score.
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

    scored = sorted(all_candidates, key=lambda l: -leg_solo_score(l))

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
            f"archetype={thesis.archetype.key} "
            f"signals_matched={signal_overlap}/{len(thesis_sigs)} "
            f"affinity={affinity_total:.2f} "
            f"conflicts={conflicts} orphans={orphans}"
        )
        combos.append(Combination(
            legs=tuple(combo),
            score=round(score, 2),
            signal_overlap_count=signal_overlap,
            archetype_affinity_total=round(affinity_total, 2),
            conflict_penalty=conflicts,
            orphan_legs=orphans,
            rationale=rationale,
        ))

    combos.sort(key=lambda c: -c.score)
    return combos[:max_combinations]
