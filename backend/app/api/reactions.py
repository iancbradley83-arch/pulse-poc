"""Public card reactions — thumbs up/down on feed cards.

Anonymous-only. Each viewer gets a first-party `pulse_anon_id` cookie (set
by middleware in main.py). One reaction per (card_id, anon_id); a second
POST upserts. Rate-limited to 30/minute per anon to prevent trivial abuse.

Why this exists: before real betting data lands, thumbs-up from shared
links is our only real signal on card quality. The `/admin/reactions`
aggregator rolls up by hook_type × bet_type × fixture so we can see which
story shapes actually resonate.

Kill switch: `PULSE_REACTIONS_ENABLED` (default true). When false, the
router short-circuits to 404 on every endpoint so the frontend also hides
the buttons (it reads the same env on boot via STATIC_VERSION request? no —
frontend just hides if the initial POST 404s).
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter

from app.services.candidate_store import CandidateStore
from app.services.feed_manager import FeedManager

logger = logging.getLogger(__name__)


def _reactions_enabled() -> bool:
    return os.getenv("PULSE_REACTIONS_ENABLED", "true").lower() == "true"


class ReactBody(BaseModel):
    # Restrict to 'up' / 'down' at the pydantic layer; the DB has a CHECK
    # constraint as a second fence.
    reaction: str = Field(..., pattern="^(up|down)$")


def create_reactions_routes(
    store: CandidateStore,
    feed: FeedManager,
    limiter: Limiter,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/cards/{card_id}/react")
    @limiter.limit("30/minute")
    async def react(
        request: Request,
        card_id: str,
        body: ReactBody,
        pulse_anon_id: Optional[str] = Cookie(None),
    ):
        if not _reactions_enabled():
            raise HTTPException(404, "reactions disabled")
        # The cookie middleware in main.py guarantees this is set before the
        # route runs, but defend against a direct curl with no cookie.
        if not pulse_anon_id:
            raise HTTPException(400, "missing pulse_anon_id cookie")
        # Validate card_id exists in the feed — prevents garbage rows if
        # someone fires random POSTs.
        card_ids = {c.id for c in feed.prematch_cards if getattr(c, "id", None)}
        if card_id not in card_ids:
            raise HTTPException(404, "unknown card_id")
        try:
            totals = await store.save_reaction(
                card_id=card_id, anon_id=pulse_anon_id, reaction=body.reaction,
            )
        except Exception as exc:
            logger.exception("[reactions] save_reaction failed: %s", exc)
            raise HTTPException(500, "failed to save reaction")
        return {"ok": True, "totals": totals}

    @router.get("/api/cards/{card_id}/reactions")
    @limiter.limit("60/minute")
    async def get_card_reactions(
        request: Request,
        card_id: str,
        pulse_anon_id: Optional[str] = Cookie(None),
    ):
        """Return totals + this viewer's current reaction so the UI can
        restore button state on reload without bleeding identity."""
        if not _reactions_enabled():
            raise HTTPException(404, "reactions disabled")
        totals = await store.reaction_totals(card_id)
        mine = await store.reaction_for_anon(card_id, pulse_anon_id) if pulse_anon_id else None
        return {"ok": True, "totals": totals, "mine": mine}

    @router.get("/admin/reactions", response_class=HTMLResponse)
    async def admin_reactions():
        if not _reactions_enabled():
            raise HTTPException(404, "reactions disabled")

        # O1: narrative-engine cards (rows that JOIN cleanly to `candidates`).
        engine_rows = await store.reaction_aggregates()

        # O1: featured BBs bypass candidate_store entirely, so their reactions
        # orphan out of the JOIN above. Report as a distinct cohort.
        orphan_rows = await store.reaction_aggregates_orphan()

        # O2: per-card click totals, joined onto the same grouping as rows.
        # We aggregate in Python (not a second GROUP BY in SQL) because
        # reaction_aggregates already collapses by (fixture, hook, bet,
        # storyline); pulling click totals for the same cohort needs a
        # parallel pass via the candidates table — cheap at current volumes.
        import aiosqlite as _aio
        cohort_clicks: dict[tuple, int] = {}
        async with _aio.connect(store._db_path) as db:
            db.row_factory = _aio.Row
            async with db.execute(
                """
                SELECT
                    c.game_id      AS fixture,
                    c.hook_type    AS hook_type,
                    c.bet_type     AS bet_type,
                    c.storyline_id AS storyline,
                    COUNT(k.id)    AS n
                FROM card_clicks k
                INNER JOIN candidates c ON c.id = k.card_id
                GROUP BY c.game_id, c.hook_type, c.bet_type, c.storyline_id
                """
            ) as cur:
                click_rows = await cur.fetchall()
        for cr in click_rows:
            key = (cr["fixture"], cr["hook_type"], cr["bet_type"], cr["storyline"])
            cohort_clicks[key] = int(cr["n"] or 0)
        for r in engine_rows:
            key = (r.get("fixture"), r.get("hook_type"), r.get("bet_type"), r.get("storyline"))
            r["clicks"] = cohort_clicks.get(key, 0)

        # Per-card clicks for featured / orphan cards (no cohort metadata).
        orphan_clicks_by_card = await store.click_totals_by_card()
        for r in orphan_rows:
            r["clicks"] = orphan_clicks_by_card.get(r["card_id"], 0)

        return HTMLResponse(_render_admin_html(engine_rows, orphan_rows))

    @router.post("/api/cards/{card_id}/click")
    @limiter.limit("60/minute")
    async def track_click(
        request: Request,
        card_id: str,
        pulse_anon_id: Optional[str] = Cookie(None),
    ):
        """Log a CTA click before the deep-link opens. Does NOT validate
        the card_id against the live feed — a card just-replaced by a
        rerun could still fire this on its way out, and we don't want to
        drop that signal. Kept intentionally cheap (single INSERT, no
        lookups) because it's called inline before `window.open`."""
        if not _reactions_enabled():
            # Piggy-back on PULSE_REACTIONS_ENABLED for now: same analytics
            # surface, same kill switch. If we ever split them we'll add a
            # dedicated PULSE_CLICK_TRACKING_ENABLED flag.
            raise HTTPException(404, "analytics disabled")
        try:
            await store.save_click(card_id=card_id, anon_id=pulse_anon_id)
        except Exception as exc:
            logger.exception("[clicks] save_click failed: %s", exc)
            # Don't 500 on analytics failure — the click still happened,
            # user is already being navigated. Best-effort log.
            return {"ok": False}
        return {"ok": True}

    return router


# ── Admin HTML rendering ─────────────────────────────────────────────

def _row_metrics(r: dict[str, Any]) -> dict[str, float]:
    """Derive display metrics for a cohort or orphan row."""
    up = int(r.get("up", 0) or 0)
    down = int(r.get("down", 0) or 0)
    clicks = int(r.get("clicks", 0) or 0)
    views = up + down  # reactions-as-views proxy; NOT impressions
    total_interactions = up + down + clicks
    up_rate = (100 * up / views) if views else 0.0
    # CTR proxy: clicks divided by reactions (up + down). We don't track
    # impressions yet so this is NOT a real CTR — header labels it clearly.
    ctr = (100 * clicks / views) if views else 0.0
    return {
        "up": up,
        "down": down,
        "clicks": clicks,
        "views": views,
        "total_interactions": total_interactions,
        "up_rate": up_rate,
        "ctr": ctr,
    }


def _fmt_txt(v: Any) -> str:
    return html.escape(str(v) if v not in (None, "") else "—")


def _engine_tbody(rows: list[dict[str, Any]]) -> str:
    """Rows + per-bet_type aggregate rows within the narrative-engine cohort."""
    # Sort by total_interactions desc before rendering.
    enriched = []
    for r in rows:
        m = _row_metrics(r)
        enriched.append({**r, **m})
    enriched.sort(key=lambda x: x["total_interactions"], reverse=True)

    # Per-bet_type aggregate at the bottom.
    per_bt: dict[str, dict[str, int]] = {}
    for r in enriched:
        bt = r.get("bet_type") or "—"
        agg = per_bt.setdefault(bt, {"up": 0, "down": 0, "clicks": 0})
        agg["up"] += r["up"]
        agg["down"] += r["down"]
        agg["clicks"] += r["clicks"]

    out: list[str] = []
    for r in enriched:
        out.append(
            "<tr>"
            f"<td>{_fmt_txt(r.get('fixture'))}</td>"
            f"<td>{_fmt_txt(r.get('hook_type'))}</td>"
            f"<td>{_fmt_txt(r.get('bet_type'))}</td>"
            f"<td>{_fmt_txt(r.get('storyline'))}</td>"
            f"<td class='num'>{r['up']}</td>"
            f"<td class='num'>{r['down']}</td>"
            f"<td class='num'>{r['clicks']}</td>"
            f"<td class='num'>{r['views']}</td>"
            f"<td class='num'>{r['up_rate']:.0f}%</td>"
            f"<td class='num'>{r['ctr']:.0f}%</td>"
            "</tr>"
        )

    # Bet-type aggregate rows.
    for bt in sorted(per_bt.keys()):
        a = per_bt[bt]
        up, down, clicks = a["up"], a["down"], a["clicks"]
        views = up + down
        up_rate = (100 * up / views) if views else 0.0
        ctr = (100 * clicks / views) if views else 0.0
        out.append(
            "<tr class='agg'>"
            f"<td colspan='2' style='text-align:right;color:#8b93a7;'>"
            f"<em>bet_type total</em></td>"
            f"<td>{html.escape(bt)}</td>"
            "<td>—</td>"
            f"<td class='num'>{up}</td>"
            f"<td class='num'>{down}</td>"
            f"<td class='num'>{clicks}</td>"
            f"<td class='num'>{views}</td>"
            f"<td class='num'>{up_rate:.0f}%</td>"
            f"<td class='num'>{ctr:.0f}%</td>"
            "</tr>"
        )
    return "".join(out)


def _orphan_tbody(rows: list[dict[str, Any]]) -> str:
    """Featured-BB cohort: sorted by total_interactions desc + one aggregate row."""
    enriched = []
    for r in rows:
        m = _row_metrics(r)
        enriched.append({**r, **m})
    enriched.sort(key=lambda x: x["total_interactions"], reverse=True)

    out: list[str] = []
    for r in enriched:
        cid = str(r.get("card_id") or "—")
        out.append(
            "<tr>"
            f"<td colspan='2'><code>{html.escape(cid)}</code></td>"
            "<td>featured_bb</td>"
            "<td>—</td>"
            f"<td class='num'>{r['up']}</td>"
            f"<td class='num'>{r['down']}</td>"
            f"<td class='num'>{r['clicks']}</td>"
            f"<td class='num'>{r['views']}</td>"
            f"<td class='num'>{r['up_rate']:.0f}%</td>"
            f"<td class='num'>{r['ctr']:.0f}%</td>"
            "</tr>"
        )
    # Single aggregate row.
    if enriched:
        up = sum(r["up"] for r in enriched)
        down = sum(r["down"] for r in enriched)
        clicks = sum(r["clicks"] for r in enriched)
        views = up + down
        up_rate = (100 * up / views) if views else 0.0
        ctr = (100 * clicks / views) if views else 0.0
        out.append(
            "<tr class='agg'>"
            "<td colspan='2' style='text-align:right;color:#8b93a7;'>"
            "<em>bet_type total</em></td>"
            "<td>featured_bb</td>"
            "<td>—</td>"
            f"<td class='num'>{up}</td>"
            f"<td class='num'>{down}</td>"
            f"<td class='num'>{clicks}</td>"
            f"<td class='num'>{views}</td>"
            f"<td class='num'>{up_rate:.0f}%</td>"
            f"<td class='num'>{ctr:.0f}%</td>"
            "</tr>"
        )
    return "".join(out)


def _render_admin_html(
    engine_rows: list[dict[str, Any]],
    orphan_rows: list[dict[str, Any]],
) -> str:
    """Two-cohort admin page.

    Cohort 1 — "Narrative-engine cards": reactions that JOIN cleanly to
    `candidates`. Grouped by (fixture, hook, bet_type, storyline).
    Cohort 2 — "Featured BB (operator-curated)": reactions on card_ids
    that aren't in `candidates` — i.e. featured BBs from the operator
    feed. Grouped by card_id (that's all the metadata we have).

    Columns: fixture / hook / bet_type / storyline / up / down / clicks /
    views / up_rate / ctr. Views = up+down (proxy, not impressions). CTR
    = clicks / views (proxy, not clicks / impressions). Rows sorted by
    total_interactions (up + down + clicks) desc; bet_type aggregate row
    at the bottom of each cohort.
    """
    def _tot(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
        up = sum(int(r.get("up", 0) or 0) for r in rows)
        down = sum(int(r.get("down", 0) or 0) for r in rows)
        clicks = sum(int(r.get("clicks", 0) or 0) for r in rows)
        return up, down, clicks

    e_up, e_down, e_clicks = _tot(engine_rows)
    o_up, o_down, o_clicks = _tot(orphan_rows)
    total_up = e_up + o_up
    total_down = e_down + o_down
    total_clicks = e_clicks + o_clicks
    total_reactions = total_up + total_down
    overall_up_rate = (100 * total_up / total_reactions) if total_reactions else 0.0
    overall_ctr = (100 * total_clicks / total_reactions) if total_reactions else 0.0

    header = (
        "<thead><tr>"
        "<th>Fixture</th>"
        "<th>Hook</th>"
        "<th>Bet type</th>"
        "<th>Storyline</th>"
        "<th style='text-align:right;'>Up</th>"
        "<th style='text-align:right;'>Down</th>"
        "<th style='text-align:right;'>Clicks</th>"
        "<th style='text-align:right;'>Views*</th>"
        "<th style='text-align:right;'>Up rate</th>"
        "<th style='text-align:right;'>CTR**</th>"
        "</tr></thead>"
    )

    engine_body = _engine_tbody(engine_rows) or (
        "<tr><td colspan='10' class='empty'>No engine-card reactions yet.</td></tr>"
    )
    orphan_body = _orphan_tbody(orphan_rows) or (
        "<tr><td colspan='10' class='empty'>No featured-BB reactions yet.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Pulse · Reactions</title>
<style>
  body {{ font: 13px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
         background:#0a0a0f; color:#e5e9f2; padding:24px; margin:0; }}
  h1 {{ font-size:18px; margin:0 0 12px; }}
  h2 {{ font-size:14px; margin:28px 0 8px; color:#cbd2e0;
       border-bottom:1px solid #1a1f2e; padding-bottom:6px; }}
  .summary {{ color:#8b93a7; margin-bottom:4px; font-size:12px; }}
  .footnote {{ color:#6b7489; margin-bottom:16px; font-size:11px; font-style:italic; }}
  table {{ border-collapse:collapse; width:100%; max-width:1200px;
          table-layout:auto; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #1a1f2e;
           text-align:left; vertical-align:top;
           white-space:nowrap; }}
  th {{ color:#8b93a7; font-weight:600; text-transform:uppercase;
       font-size:10px; letter-spacing:0.5px; position:sticky; top:0;
       background:#0a0a0f; }}
  tr:hover td {{ background:#11141c; }}
  tr.agg td {{ background:#0e1119; border-top:1px solid #1a1f2e;
             border-bottom:1px solid #2a2f40; font-weight:600; }}
  td.num {{ font-variant-numeric: tabular-nums; text-align:right; }}
  .empty {{ color:#8b93a7; padding:24px; text-align:center; }}
  code {{ font-size:11px; color:#9fb3c8; background:#11141c;
         padding:2px 6px; border-radius:3px; }}
</style></head>
<body>
  <h1>Card reactions</h1>
  <div class="summary">
    {len(engine_rows)} engine cohorts · {len(orphan_rows)} featured cards ·
    {total_up} up · {total_down} down ·
    overall {overall_up_rate:.0f}% up ·
    {total_clicks} clicks · {overall_ctr:.0f}% CTR
  </div>
  <div class="footnote">
    *Views = up + down reactions (proxy — we don't track impressions yet).
    **CTR vs reactions (not impressions) = clicks / (up + down).
  </div>

  <h2>Narrative-engine cards</h2>
  <table>{header}<tbody>{engine_body}</tbody></table>

  <h2>Featured BB (operator-curated)</h2>
  <table>{header}<tbody>{orphan_body}</tbody></table>
</body></html>"""
