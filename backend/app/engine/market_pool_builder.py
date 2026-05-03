"""Phase 3b: per-fixture market pool selection by gradient importance.

Pure-function module that turns the raw `Markets[]` list from a Rogue
`includeMarkets="all"` event payload into the curated pool of markets we
expose to the engine for a given fixture. The pool is sized by the
fixture's gradient importance score:

  * score ≈ 1.0  → top-N per group ceiling (top fixtures get full breadth)
  * score ≈ 0.5  → middle interpolation
  * score ≈ 0.0  → top-N per group floor (tail fixtures stay cheap)

`MarketCatalog.ALLOW`-equivalent floor markets always survive — they are
the anchor markets every card needs (1X2, OU 2.5, BTTS) regardless of
group composition.

## Why this shape (vs. inventing a "narrative-fit" scorer)

The Rogue OpenAPI exposes `InMarketGroups[].MarketOrder` per market —
the operator's merchandising team has already ranked markets within each
group. This module sorts by `MarketOrder` ascending and takes top-N per
group. We don't reinvent ordering.

## Why not flip routing live yet

Per `feedback_calibrate_before_scoring`: capture-and-observe before
behaviour-routing. Phase 3a (PR #118) emits the per-group distribution.
Phase 3b (this module) is the routing layer — wired but dark behind
`PULSE_DEEP_MARKET_EXPANSION_ENABLED=false` until 1-2 cycles of Phase 3a
data confirm the per-group distributions match what we sized against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class PoolGradientConfig:
    """Per-group cap + which groups to skip / always include.

    `top_n_floor` = cap when fixture importance_score = 0.0
    `top_n_ceil`  = cap when fixture importance_score = 1.0

    `skip_groups` are double-count buckets (`All Markets`) or unranked
    buckets (`Special` whose MarketOrder is 999999 — operator hasn't
    ranked them; including would bias toward random selection).

    `always_include_market_names` is the ALLOW-floor: markets matching
    any of these names (case-insensitive substring) are always pooled
    regardless of group rank.
    """
    top_n_floor: int = 2
    top_n_ceil: int = 10
    skip_groups: frozenset[str] = field(
        default_factory=lambda: frozenset({"All Markets", "Special"})
    )
    always_include_market_names: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "match result", "1x2", "match winner",
            "total goals", "over/under", "match goals",
            "both teams to score", "btts",
            "double chance",
            "asian handicap", "draw no bet",
        })
    )


def cap_for_score(score: Optional[float], cfg: PoolGradientConfig) -> int:
    """Linear-interpolate `top_n_floor → top_n_ceil` across `[0, 1]`.

    `score=None` (no Phase 1 signals stamped) is treated as 1.0 — fail
    open, mirroring `GradientRoutingConfig.for_score()` in
    `importance_scorer.py`. Result is clamped to a non-negative int.
    """
    if score is None:
        return cfg.top_n_ceil
    s = max(0.0, min(1.0, float(score)))
    cap = cfg.top_n_floor + s * (cfg.top_n_ceil - cfg.top_n_floor)
    return max(0, int(round(cap)))


def _market_belongs_to_floor(market: dict[str, Any],
                              floor_names: frozenset[str]) -> bool:
    if not floor_names:
        return False
    name = (market.get("Name") or market.get("MarketName") or "").lower().strip()
    if not name:
        return False
    if name in floor_names:
        return True
    return any(name.startswith(f) for f in floor_names)


def _per_group_index(markets: list[dict[str, Any]]
                      ) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    """Build `group_name -> [(market_order, market_dict), ...]` ascending.

    Mirrors the live shape from `_coerce_group_entry` in the observer:
    `InMarketGroups[].MarketOrder` is the per-group rank.
    """
    by_group: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for m in markets:
        in_groups = m.get("InMarketGroups") or m.get("NewInMarketGroups") or []
        if not in_groups:
            continue
        for entry in in_groups:
            if isinstance(entry, dict):
                gname = (
                    entry.get("Name") or entry.get("GroupName")
                    or entry.get("name") or entry.get("groupName")
                )
                gorder: Any = None
                for k in ("MarketOrder", "Order", "MarketGroupOrder"):
                    v = entry.get(k)
                    if isinstance(v, (int, float)):
                        gorder = v
                        break
                if gorder is None:
                    gorder = m.get("MarketGroupOrder")
            else:
                gname = str(entry) if entry else None
                gorder = m.get("MarketGroupOrder")
            if not gname or not isinstance(gorder, (int, float)):
                continue
            by_group.setdefault(gname, []).append((float(gorder), m))
    for gname in by_group:
        by_group[gname].sort(key=lambda kv: kv[0])
    return by_group


def build_pool(
    markets: list[dict[str, Any]],
    importance_score: Optional[float],
    *,
    cfg: Optional[PoolGradientConfig] = None,
) -> list[dict[str, Any]]:
    """Curated per-fixture market pool, sized by gradient importance.

    Returns the deduplicated list of market dicts that should be
    surfaced to candidate-builder / combo-builder for this fixture.
    Order of the returned list is not significant — callers index by
    `_id`.
    """
    cfg = cfg or PoolGradientConfig()
    if not markets:
        return []
    cap = cap_for_score(importance_score, cfg)
    selected: dict[str, dict[str, Any]] = {}
    for m in markets:
        if _market_belongs_to_floor(m, cfg.always_include_market_names):
            mid = m.get("_id") or m.get("Id")
            if mid:
                selected[str(mid)] = m
    if cap <= 0:
        return list(selected.values())
    by_group = _per_group_index(markets)
    for gname, ranked in by_group.items():
        if gname in cfg.skip_groups:
            continue
        for _order, m in ranked[:cap]:
            mid = m.get("_id") or m.get("Id")
            if mid:
                selected[str(mid)] = m
    return list(selected.values())


def pool_summary(pool: list[dict[str, Any]]) -> dict[str, Any]:
    """Diagnostic summary of a built pool — for the admin endpoint."""
    by_group_count: dict[str, int] = {}
    bb_eligible_selections = 0
    total_selections = 0
    for m in pool:
        for entry in (m.get("InMarketGroups") or m.get("NewInMarketGroups") or []):
            if isinstance(entry, dict):
                gname = (
                    entry.get("Name") or entry.get("GroupName")
                    or entry.get("name") or entry.get("groupName")
                )
            else:
                gname = str(entry) if entry else None
            if gname:
                by_group_count[gname] = by_group_count.get(gname, 0) + 1
        for sel in (m.get("Selections") or []):
            total_selections += 1
            if sel.get("IsBetBuilderAvailable"):
                bb_eligible_selections += 1
    return {
        "total_markets": len(pool),
        "total_selections": total_selections,
        "bb_eligible_selections": bb_eligible_selections,
        "markets_per_group": dict(
            sorted(by_group_count.items(), key=lambda kv: -kv[1])
        ),
    }
