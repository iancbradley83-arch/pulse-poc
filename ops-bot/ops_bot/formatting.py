"""
Response format helpers.

All output must match Appendix A of DESIGN.md exactly — monospace-friendly,
no emoji. Reviewer will diff against the spec.
"""
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _age_str(iso_str: str) -> str:
    """Return a human-readable age string like '2h ago' or '4m ago'."""
    try:
        # Railway timestamps are ISO 8601 with Z suffix.
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "just now"
        if total_seconds < 60:
            return f"{total_seconds}s ago"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except Exception:
        return iso_str


def format_help() -> str:
    return (
        "pulse-ops-bot — commands\n"
        "\n"
        "stage 1 (read-only)\n"
        "  /status        pulse health + cost + deploy + cards/$ per card\n"
        "  /cost [days]   daily LLM spend, default last 3\n"
        "  /breakdown     today's spend by kind + $/card KPIs\n"
        "\n"
        "stage 2 coming: /feed /card /embed /logs /runbook /env\n"
        "stage 3 coming: /pause /resume /rerun /flag /redeploy /blacklist /snooze\n"
        "stage 4 coming: /preview /restore /incident /contact"
    )


def format_status(
    health: Optional[Dict[str, Any]],
    cost: Optional[Dict[str, Any]],
    deployment: Optional[Dict[str, Any]],
    feed: Optional[Dict[str, Any]],
    engine_vars: Optional[Dict[str, str]],
    pulse_unreachable: bool = False,
    railway_unreachable: bool = False,
    check_age_seconds: int = 0,
    cost_detail: Optional[Dict[str, Any]] = None,
) -> str:
    lines: List[str] = []

    # Pulse health line.
    if health is not None:
        ok = health.get("ok", False)
        pulse_state = "ok" if ok else "DOWN"
        lines.append(f"Pulse: {pulse_state}")
    else:
        lines.append("Pulse: DOWN")

    # Cost line.
    if cost is not None:
        total_usd = cost.get("total_usd", 0.0)
        total_calls = cost.get("total_calls", 0)
        limit_usd = cost.get("limit_usd", 3.0)
        pct = math.floor((total_usd / limit_usd * 100)) if limit_usd > 0 else 0
        lines.append(
            f"Cost: ${total_usd:.2f} / ${limit_usd:.2f} ({pct}%) — {total_calls} calls today"
        )
    else:
        lines.append("Cost: (unavailable)")

    # Deploy line.
    if deployment is not None:
        status = deployment.get("status", "UNKNOWN")
        commit = deployment.get("commitHash", "")[:7] or "unknown"
        created_at = deployment.get("createdAt", "")
        age = _age_str(created_at) if created_at else "unknown"
        lines.append(f"Deploy: {status} — {commit} — {age}")
    else:
        lines.append("Deploy: (unavailable)")

    # Feed line.
    if feed is not None:
        count = feed.get("count", 0)
        lines.append(f"Feed: {count} cards")
    else:
        lines.append("Feed: (unavailable)")

    # Engine kill-switch states.
    if engine_vars is not None:
        def _flag(var: str, label: str) -> str:
            val = engine_vars.get(var, "")
            state = "on" if val.lower() in ("true", "1", "yes") else "off"
            return f"{label}={state}"

        rerun = _flag("PULSE_RERUN_ENABLED", "rerun")
        news = _flag("PULSE_NEWS_INGEST_ENABLED", "news")
        storylines = _flag("PULSE_TIERED_FRESHNESS_ENABLED", "storylines")
        lines.append(f"Engine: {rerun}  {news}  {storylines}")
    else:
        lines.append("Engine: (unavailable)")

    # Cards-in-feed + $/card KPI line (PR #84 enrichment, optional).
    if cost_detail is not None:
        cards_feed = cost_detail.get("cards_in_feed_now")
        total_usd = float(cost_detail.get("total_usd", 0.0))
        if cards_feed is not None:
            kpi = f"Cards: {cards_feed} in feed"
            if isinstance(cards_feed, int) and cards_feed > 0 and total_usd > 0:
                kpi += f"  $/card: ${total_usd / cards_feed:.4f}"
            lines.append(kpi)

    lines.append("")
    lines.append(f"last check: {check_age_seconds}s ago")

    if pulse_unreachable:
        lines.append("(Pulse unreachable)")
    if railway_unreachable:
        lines.append("(Railway API unreachable)")

    return "\n".join(lines)


def format_cost(days_data: List[Dict[str, Any]], num_days: int) -> str:
    """
    Format the /cost [days] response.

    Expected shape of each day dict:
      {"date": "YYYY-MM-DD", "usd": float, "calls": int, "limit_usd": float}
    """
    lines: List[str] = [f"Daily LLM spend (last {num_days} days)", ""]

    for row in days_data:
        date = row.get("date", "unknown")
        usd = float(row.get("usd", 0.0))
        calls = int(row.get("calls", 0))
        limit = float(row.get("limit_usd", 3.0))

        if limit > 0:
            pct = math.floor(usd / limit * 100)
            pct_str = f"{pct}%"
        else:
            pct_str = "n/a"

        lines.append(f"{date}  ${usd:.2f} / ${limit:.2f}  {pct_str}   {calls} calls")

    return "\n".join(lines)


def format_cost_alert(spend_usd: float, threshold_usd: float, limit_usd: float = 3.0) -> str:
    """Format a push alert for a crossed cost threshold."""
    pct = math.floor(spend_usd / limit_usd * 100) if limit_usd > 0 else 0
    return (
        f"[ops-bot] CRITICAL — cost crossed ${threshold_usd:.2f} today\n"
        f"spend: ${spend_usd:.2f} / ${limit_usd:.2f} ({pct}%)\n"
        f"\n"
        f"tripwire fires at ${limit_usd:.2f}. consider /pause if scout is leaking."
    )


def format_boot_ping(pulse_ok: bool, spend_usd: float, limit_usd: float = 3.0) -> str:
    """Format the boot ping message sent on startup."""
    pulse_state = "ok" if pulse_ok else "down"
    return (
        f"[ops-bot] online — restart\n"
        f"Pulse: {pulse_state}  cost: ${spend_usd:.2f} / ${limit_usd:.2f}"
    )


def format_breakdown(detail: Dict[str, Any]) -> str:
    """
    Format the /breakdown response from /admin/cost.json?detail=1.

    Renders today's spend split by kind, plus card counts and per-card KPIs.
    Tolerates missing/null enrichment fields (the new telemetry can be cold).
    """
    today = ""
    if detail.get("days"):
        today = detail["days"][0].get("date", "") or ""

    total_usd = float(detail.get("total_usd", 0.0))
    limit_usd = float(detail.get("limit_usd", 3.0))
    total_calls = int(detail.get("total_calls", 0))
    pct = math.floor(total_usd / limit_usd * 100) if limit_usd > 0 else 0

    lines: List[str] = []
    header = "Daily breakdown" + (f" — {today}" if today else "")
    lines.append(header)
    lines.append(f"Total: ${total_usd:.2f} / ${limit_usd:.2f}  ({pct}%) — {total_calls} calls")
    lines.append("")

    by_kind = detail.get("by_kind") or {}
    if by_kind:
        lines.append("By kind:")
        rows = sorted(
            by_kind.items(),
            key=lambda x: -float((x[1] or {}).get("usd", 0.0)),
        )
        for kind, agg in rows:
            agg = agg or {}
            usd = float(agg.get("usd", 0.0))
            calls = int(agg.get("calls", 0))
            lines.append(f"  {kind:22} {calls:4d} calls   ${usd:.4f}")
    else:
        lines.append("By kind: (no per-kind data yet — engine cycles will populate)")
    lines.append("")

    cards_feed = detail.get("cards_in_feed_now")
    cards_today = detail.get("unique_cards_published_today")
    republishes = detail.get("republish_events_today")
    cache_hits = detail.get("rewrite_cache_hits_today")

    cards_parts: List[str] = []
    if cards_feed is not None:
        cards_parts.append(f"{cards_feed} in feed")
    if cards_today is not None:
        cards_parts.append(f"{cards_today} unique today")
    if republishes is not None:
        cards_parts.append(f"{republishes} publish events")
    if cards_parts:
        lines.append("Cards: " + " · ".join(cards_parts))

    if isinstance(cards_feed, int) and cards_feed > 0 and total_usd > 0:
        lines.append(f"$/card in feed:      ${total_usd / cards_feed:.4f}")
    if isinstance(cards_today, int) and cards_today > 0 and total_usd > 0:
        lines.append(f"$/unique card today: ${total_usd / cards_today:.4f}")

    if cache_hits is not None:
        lines.append(f"Rewrite cache hits today: {cache_hits}")

    if (
        isinstance(republishes, int)
        and isinstance(cards_today, int)
        and cards_today > 0
        and republishes > 3 * cards_today
    ):
        lines.append("")
        lines.append(
            "(today has heavy republish churn — likely redeploy boots; "
            "steady-state $/card is lower)"
        )

    return "\n".join(lines)
