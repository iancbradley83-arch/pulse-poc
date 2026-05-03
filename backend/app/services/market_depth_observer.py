"""Market-depth observability — Phase 3a of fixture-importance.md.

Pure-function module that introspects the per-fixture market data Rogue
already returns inside `fetch_soccer_snapshot` (via
``includeMarkets="all"``) and reports per-group market counts plus
``MarketGroupOrder`` distribution for the top-N gradient fixtures.

Why this is upstream-first (HR1 lesson, captured 2026-04-29 on
polysportsbook): the Rogue OpenAPI spec exposes both ``InMarketGroups``
and ``MarketGroupOrder`` per market — the operator's merchandising team
already ranks markets within each group. Phase 3b will sample top-N per
group by ascending ``MarketGroupOrder`` rather than inventing a
"narrative-fit" scoring system. This module is the calibration step that
sizes those caps against real distributions before any routing flips on.

Per ``feedback_calibrate_before_scoring.md``: capture-and-observe before
behaviour-routing on a score. No new Rogue calls (we read the same
``raw_detailed`` dicts the catalogue loader already collected). No
persisted state. No engine behaviour change. Logs only, plus an admin
endpoint for ad-hoc queries from outside the cycle.
"""
from __future__ import annotations

import logging
import statistics
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app.models.schemas import Game

logger = logging.getLogger(__name__)


def _coerce_group_entry(entry: Any, fallback_order: Any) -> tuple[Optional[str], Optional[float]]:
    """Pull ``(group_name, group_order)`` out of one ``InMarketGroups`` entry.

    Defensive: Rogue returns ``InMarketGroups`` as either a list of strings
    or a list of ``InMarketGroupsModel`` objects (per the OpenAPI spec).
    Object form carries its own per-group rank — the live shape uses
    ``MarketOrder`` (verified 2026-05-03 against MUN vs LIV); ``Order``
    and ``MarketGroupOrder`` are accepted as defensive aliases. Per-group
    rank wins over the market-level ``MarketGroupOrder`` (the latter is
    a GLOBAL rank across all groups, not the per-group rank we want when
    sampling within `Goals`, `Corners`, etc.).
    """
    if isinstance(entry, dict):
        gname = (
            entry.get("Name")
            or entry.get("GroupName")
            or entry.get("name")
            or entry.get("groupName")
        )
        gorder: Any = None
        for key in ("MarketOrder", "Order", "MarketGroupOrder"):
            v = entry.get(key)
            if isinstance(v, (int, float)):
                gorder = v
                break
        if gorder is None:
            gorder = fallback_order
    else:
        gname = str(entry) if entry else None
        gorder = fallback_order
    if isinstance(gorder, (int, float)):
        return (gname, float(gorder))
    return (gname, None)


def _group_distribution(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each MarketGroup present, count markets and summarize order.

    A market can appear in multiple groups (``InMarketGroups`` is an
    array). Each group bucket counts the market once per membership.
    Returns dict keyed on group name with ``market_count``,
    ``order_min``, ``order_max``, ``order_median`` per group.
    """
    by_group: dict[str, list[float]] = {}
    by_group_unranked: dict[str, int] = {}
    for m in markets:
        in_groups = m.get("InMarketGroups") or m.get("NewInMarketGroups") or []
        market_order = m.get("MarketGroupOrder")
        if not in_groups:
            continue
        for entry in in_groups:
            gname, gorder = _coerce_group_entry(entry, market_order)
            if not gname:
                continue
            if gorder is None:
                by_group_unranked[gname] = by_group_unranked.get(gname, 0) + 1
                by_group.setdefault(gname, [])
            else:
                by_group.setdefault(gname, []).append(gorder)
    out: dict[str, dict[str, Any]] = {}
    for gname, orders in by_group.items():
        unranked = by_group_unranked.get(gname, 0)
        ranked_count = len(orders)
        out[gname] = {
            "market_count": ranked_count + unranked,
            "ranked_market_count": ranked_count,
            "unranked_market_count": unranked,
            "order_min": min(orders) if orders else None,
            "order_max": max(orders) if orders else None,
            "order_median": (
                round(statistics.median(orders), 2) if orders else None
            ),
        }
    return out


def _participant_name(participants: list[dict[str, Any]], idx: int) -> str:
    if idx >= len(participants):
        return "?"
    p = participants[idx] or {}
    return str(p.get("Name") or p.get("name") or "?")


def observe_fixture(
    event: dict[str, Any],
    *,
    game: Optional["Game"] = None,
) -> dict[str, Any]:
    """Build a market-depth report for a single fixture.

    ``event`` is the raw Rogue event dict from
    ``client.get_event(includeMarkets="all")``. ``game`` (when supplied)
    annotates the report with importance score, league_order, and the
    operator-featured flag — pass ``None`` for ad-hoc admin queries
    where we don't have a Game in memory.
    """
    markets = event.get("Markets") or []
    settings = event.get("Settings") or {}
    participants = event.get("Participants") or []

    home = _participant_name(participants, 0)
    away = _participant_name(participants, 1)

    return {
        "event_id": str(event.get("_id") or event.get("Id") or ""),
        "label": f"{home} vs {away}",
        "importance_score": (
            game.importance_score if game is not None else None
        ),
        "league_order": (
            game.league_order if game is not None else event.get("LeagueOrder")
        ),
        "is_operator_featured": (
            game.is_operator_featured if game is not None else None
        ),
        "total_active_markets_count": (
            event.get("TotalActiveMarketsCount") or len(markets)
        ),
        "is_bet_builder_enabled": bool(
            settings.get("IsBetBuilderEnabled", False)
        ),
        "groups_present": sorted(event.get("MarketGroups") or []),
        "groups_detail": _group_distribution(markets),
    }


def log_fixture_report(report: dict[str, Any]) -> None:
    """Emit a multi-line log block per observed fixture."""
    label = report.get("label", "?")
    score = report.get("importance_score")
    score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
    bb = "yes" if report.get("is_bet_builder_enabled") else "no"
    total = report.get("total_active_markets_count", 0)

    logger.info(
        "[market_depth_observe] %s  importance=%s  total_markets=%d  bb_enabled=%s",
        label, score_str, total, bb,
    )

    detail = report.get("groups_detail") or {}
    if not detail:
        logger.info(
            "[market_depth_observe]   (no group data — InMarketGroups absent on every market)"
        )
        return

    sorted_groups = sorted(
        detail.items(), key=lambda kv: -kv[1].get("market_count", 0)
    )
    parts = []
    for gname, d in sorted_groups:
        omin = d.get("order_min")
        omax = d.get("order_max")
        if omin is None or omax is None:
            order_str = "no-order"
        else:
            order_str = f"order {omin:.0f}-{omax:.0f}"
        parts.append(f"{gname}({d.get('market_count', 0)}, {order_str})")
    logger.info("[market_depth_observe]   groups: %s", " ".join(parts))


def observe_top_fixtures(
    games: list["Game"],
    raw_by_id: dict[str, dict[str, Any]],
    *,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Pick top-N games by importance_score; build + log a report each.

    Returns the reports as a list (the admin endpoint exposes them as
    JSON). Silent no-op when ``games`` or ``raw_by_id`` is empty, or no
    raw payloads matched the top-N games.
    """
    if not games or not raw_by_id:
        return []

    ranked = sorted(
        games,
        key=lambda g: -(
            g.importance_score if g.importance_score is not None else 0.0
        ),
    )
    reports: list[dict[str, Any]] = []
    for g in ranked[:top_n]:
        raw = raw_by_id.get(g.id)
        if not raw:
            continue
        report = observe_fixture(raw, game=g)
        log_fixture_report(report)
        reports.append(report)

    if not reports:
        logger.info(
            "[market_depth_observe] no top fixtures matched raw payloads (n=%d games, %d raw)",
            len(games), len(raw_by_id),
        )
    return reports
