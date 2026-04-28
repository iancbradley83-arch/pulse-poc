"""
Response format helpers.

All output must match Appendix A of DESIGN.md exactly — monospace-friendly,
no emoji. Reviewer will diff against the spec.
"""
import math
import re as _re
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
        "pulse-ops-bot — Pulse ops from your phone\n"
        "\n"
        "Telegram console for the Pulse content widget. Monitor health,\n"
        "cost, and deploys. Inspect cards and operator embeds. Pull logs\n"
        "and runbook sections — all without opening a laptop. Read-only\n"
        "today; pause / resume / redeploy / blacklist land in stage 3.\n"
        "\n"
        "stage 1 (read-only)\n"
        "  /status              pulse health + cost + deploy + cards/$ per card\n"
        "  /cost [days]         daily LLM spend, default last 3\n"
        "  /breakdown           today's spend by kind + $/card KPIs\n"
        "\n"
        "stage 2 (visibility)\n"
        "  /feed                feed audit — hook mix, league mix, missing prices\n"
        "  /cards [page]        paginated card list (5 per page, defaults to 1)\n"
        "  /card <id>           full card detail (id or 8-char prefix)\n"
        "  /embed [slug]        embed config; bare lists configured slugs\n"
        "  /logs [n]            last n WARN/ERROR from pulse-poc (default 20)\n"
        "  /runbook [topic]     RUNBOOK.md section; bare lists topics\n"
        "  /env <key>           current env var on pulse-poc (secrets scrubbed)\n"
        "\n"
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


# ---------------------------------------------------------------------------
# Stage 2 — feed audit + pagination
# ---------------------------------------------------------------------------

def format_feed_audit(summary: Dict[str, Any]) -> str:
    """Format a feed audit summary dict from feed_audit.build_feed_summary()."""
    lines: List[str] = [f"feed audit — {summary['total']} cards", ""]

    if summary["by_hook_type"]:
        lines.append("by hook_type:")
        for hook, count in summary["by_hook_type"]:
            lines.append(f"  {hook:<20} {count}")
        lines.append("")

    if summary["by_league"]:
        lines.append("by league (top 5):")
        for league, count in summary["by_league"]:
            lines.append(f"  {league:<30} {count}")
        lines.append("")

    lines.append(f"missing prices : {summary['missing_price']}")
    lines.append(f"suspended      : {summary['suspended']}")
    avg = summary["avg_relevance"]
    lines.append(f"avg relevance  : {avg:.2f}" if avg is not None else "avg relevance  : n/a")

    return "\n".join(lines)


def _team_name(team: Any) -> str:
    """Extract a usable string from a team field that may be dict-or-string."""
    if isinstance(team, dict):
        return team.get("name") or team.get("short_name") or "?"
    return str(team) if team else "?"


def _truncate_at_word(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1]
    space = cut.rfind(" ")
    if space > max_len * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;.") + "…"


def _card_block(card: Dict[str, Any]) -> str:
    """Render a single feed card as a 3- to 4-line block, phone-readable."""
    cid = (card.get("id") or "")[:8] or "????????"
    hook = card.get("hook_type") or card.get("bet_type") or "?"

    odds = card.get("total_odds")
    odds_str = f"{odds:.2f}" if odds is not None else "no price"

    suspended = card.get("suspended", False)
    flag = "  [SUSPENDED]" if suspended else ""

    game = card.get("game") or {}
    home = _team_name(game.get("home_team") or game.get("home"))
    away = _team_name(game.get("away_team") or game.get("away"))
    league_obj = game.get("league") or {}
    league = (
        (league_obj.get("name") if isinstance(league_obj, dict) else None)
        or card.get("league")
        or game.get("league_name")
        or ""
    )
    game_line = f"{home} vs {away}"
    if league:
        game_line += f" · {league}"

    narrative = (card.get("narrative_hook") or card.get("headline") or "").strip()
    narrative = _truncate_at_word(narrative, 140)

    lines = [f"[{cid}] · {hook} · {odds_str}{flag}"]
    if home != "?" or away != "?":
        lines.append(game_line)
    if narrative:
        lines.append(narrative)
    return "\n".join(lines)


def format_feed_page(
    cards: List[Dict[str, Any]],
    page: int,
    total_pages: int,
    total_cards: int,
) -> str:
    """Format a single page of feed cards as multi-line blocks separated by blank lines."""
    if not cards:
        return f"no such page (feed has {total_pages} page(s))"

    blocks = [_card_block(c) for c in cards]
    body = "\n\n".join(blocks)

    # Footer with tappable nav commands (Telegram makes /commands in message
    # text tappable on iOS / Android / desktop).
    # Telegram makes a single token like /cards_2 tappable as a command.
    # Plain '/cards 2' only highlights '/cards' and tapping doesn't pass the arg.
    nav_parts: List[str] = []
    if page > 1:
        nav_parts.append(f"prev: /cards_{page - 1}")
    nav_parts.append(f"page {page} of {total_pages}")
    if page < total_pages:
        nav_parts.append(f"next: /cards_{page + 1}")
    nav = "  ·  ".join(nav_parts)

    footer = f"\n\n— {nav} —\n{total_cards} cards in feed · /card <id> for detail"
    return body + footer


# ---------------------------------------------------------------------------
# Stage 2 — card detail
# ---------------------------------------------------------------------------

def format_card_detail(card: Dict[str, Any]) -> str:
    """Render full card detail. Skip lines for absent fields."""
    lines: List[str] = []

    card_id = card.get("id") or "unknown"
    bet_type = card.get("bet_type") or card.get("hook_type") or "unknown"
    lines.append(f"Card {card_id[:8]} — {bet_type}")

    game = card.get("game") or {}
    home = _team_name(game.get("home_team") or game.get("home"))
    away = _team_name(game.get("away_team") or game.get("away"))
    league_obj = game.get("league") or {}
    league = (
        (league_obj.get("name") if isinstance(league_obj, dict) else None)
        or card.get("league")
        or game.get("league_name")
        or ""
    )
    kickoff = game.get("kickoff_time") or game.get("start_time") or ""
    if home != "?" or away != "?":
        game_line = f"Game: {home} vs {away}"
        if league or kickoff:
            parts = []
            if league:
                parts.append(league)
            if kickoff:
                parts.append(kickoff)
            game_line += f" ({' · '.join(parts)})"
        lines.append(game_line)

    hook_type = card.get("hook_type")
    if hook_type:
        lines.append(f"Hook: {hook_type}")

    narrative = card.get("narrative_hook") or ""
    if narrative:
        lines.append(f"Narrative: {narrative}")

    headline = card.get("headline") or ""
    if headline and headline != narrative:
        lines.append(f"Headline: {headline}")

    legs = card.get("legs") or []
    if legs:
        lines.append("Legs:")
        for i, leg in enumerate(legs, 1):
            selection = (
                leg.get("selection")
                or leg.get("description")
                or leg.get("market")
                or "?"
            )
            price = leg.get("price")
            price_str = f" @ {price:.2f}" if price is not None else ""
            lines.append(f"  {i}. {selection}{price_str}")

    total_odds = card.get("total_odds")
    if total_odds is not None:
        lines.append(f"Total odds: {total_odds:.2f}")

    relevance = card.get("relevance_score")
    if relevance is not None:
        lines.append(f"Relevance: {relevance:.2f}")

    suspended = card.get("suspended", False)
    lines.append(f"Suspended: {'yes' if suspended else 'no'}")

    deep_link = card.get("deep_link") or card.get("deeplink") or card.get("url") or ""
    if deep_link:
        lines.append(f"Deep link: {deep_link}")

    published = card.get("published_at") or card.get("created_at") or ""
    if published:
        lines.append(f"Published: {_age_str(published)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 2 — embed detail
# ---------------------------------------------------------------------------

def format_embed(embed: Dict[str, Any]) -> str:
    """Render embed detail for /embed <slug>."""
    lines: List[str] = []

    slug = embed.get("slug") or "unknown"
    lines.append(f"Embed: {slug}")

    token = embed.get("token") or ""
    if token:
        scrubbed = token[:8] + "***"
        lines.append(f"Token: {scrubbed}")

    origins = embed.get("allowed_origins") or []
    if origins:
        lines.append(f"Domains: {', '.join(origins)}")

    theme = embed.get("theme_overrides") or {}
    theme_count = len(theme) if isinstance(theme, dict) else 0
    lines.append(f"Theme overrides: {theme_count}")

    created_at = embed.get("created_at") or ""
    if created_at:
        lines.append(f"Created: {_age_str(created_at)}")

    last_served = embed.get("last_served_at") or embed.get("last_served") or ""
    if last_served:
        lines.append(f"Last served: {_age_str(last_served)}")

    active = embed.get("active")
    if active is not None:
        lines.append(f"Active: {'yes' if active else 'no'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 2 — logs
# ---------------------------------------------------------------------------

# Strip the duplicate "YYYY-MM-DD HH:MM:SS,mmm INFO" prefix from message body.
_PY_PREFIX_RE = _re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[,.]\d+\s+"
    r"\[?(?:CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\]?\s+"
)


def format_logs(entries: List[Dict[str, str]], n: int) -> str:
    """Format WARN/ERROR log entries — compact, prefix-stripped, level-coded."""
    if not entries:
        return f"no warn/error in last deployment (requested {n})"

    lines: List[str] = [f"last {len(entries)} warn/error from pulse-poc", ""]
    for entry in entries:
        ts = entry.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_display = dt.strftime("%H:%M:%S")
        except Exception:
            ts_display = ts[:8]

        severity = (entry.get("severity") or "").upper() or "?"
        message = (entry.get("message") or "").strip()

        # Drop the inner Python timestamp + level prefix (avoid duplication).
        message = _PY_PREFIX_RE.sub("", message, count=1)
        if len(message) > 240:
            message = message[:237] + "..."

        lines.append(f"{ts_display} {severity:<7} {message}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 2 — env var
# ---------------------------------------------------------------------------

_SECRET_PATTERN = _re.compile(r"(?i)(token|secret|key|pass|jwt|api)")


def format_env_var(key: str, value: Optional[str], railway_unreachable: bool = False) -> str:
    """Format an env var lookup result. Scrubs secret-looking keys."""
    if railway_unreachable:
        return "(Railway API unreachable)"

    if value is None:
        return f"{key} is not set"

    if _SECRET_PATTERN.search(key):
        scrubbed = value[:8] + "***" if len(value) >= 8 else value[:4] + "***"
        return f"{key} = {scrubbed}  <scrubbed>"

    return f"{key} = {value}"
