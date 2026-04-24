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
        rows = await store.reaction_aggregates()
        # Stage 5: join per-card click totals so the admin table shows
        # which cards drove through-to-slip opens, not just thumbs. We
        # aggregate in Python (not a second GROUP BY in SQL) because
        # reaction_aggregates already collapses by (fixture, hook, bet,
        # storyline) and the click count for THAT cohort needs a second
        # pass via the candidates table — cheap at current volumes (~30
        # cards per cycle).
        click_totals = await store.click_totals()
        # Pull fixture/hook/bet/storyline for every clicked card so we
        # can re-aggregate click totals onto the same cohort grouping
        # that reactions use.
        # Reuse CandidateStore._connect so journal_mode=DELETE pragma is
        # applied — raw aiosqlite.connect throws disk-I/O errors on the
        # Railway NFS-style volume (see feedback_sqlite_railway_volume).
        import aiosqlite as _aio
        cohort_clicks: dict[tuple, int] = {}
        async with store._connect() as db:
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
                LEFT JOIN candidates c ON c.id = k.card_id
                GROUP BY c.game_id, c.hook_type, c.bet_type, c.storyline_id
                """
            ) as cur:
                click_rows = await cur.fetchall()
        orphan_clicks = 0
        for cr in click_rows:
            key = (cr["fixture"], cr["hook_type"], cr["bet_type"], cr["storyline"])
            n = int(cr["n"] or 0)
            # The LEFT JOIN pulls NULL for every column when the click's
            # card_id doesn't match any candidate row (featured BBs, which
            # bypass the candidate store by design). Those collapse into
            # one (None, None, None, None) GROUP BY bucket — route them
            # to the orphan counter so they show in the summary bar
            # instead of being silently attached to a ghost cohort that
            # will never appear in `rows`.
            if key == (None, None, None, None):
                orphan_clicks += n
            else:
                cohort_clicks[key] = n
        for r in rows:
            key = (r.get("fixture"), r.get("hook_type"), r.get("bet_type"), r.get("storyline"))
            r["clicks"] = cohort_clicks.get(key, 0)
        return HTMLResponse(_render_admin_html(rows, orphan_clicks=orphan_clicks))

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


def _render_admin_html(
    rows: list[dict[str, Any]],
    *,
    orphan_clicks: int = 0,
) -> str:
    """Simple admin page, sorted by total reactions desc.

    Columns: fixture, hook_type, bet_type, storyline, up, down, total, up_rate, clicks.
    Stage 5 added the clicks column (deep-link opens from card CTA).
    No auth (POC); same posture as /admin/candidates.
    """
    total_rows = len(rows)
    total_up = sum(r.get("up", 0) for r in rows)
    total_down = sum(r.get("down", 0) for r in rows)
    total_clicks = sum(r.get("clicks", 0) for r in rows) + int(orphan_clicks or 0)
    total_all = total_up + total_down
    overall_up_rate = (100 * total_up / total_all) if total_all else 0.0

    tbody = []
    for r in rows:
        up = int(r.get("up", 0) or 0)
        down = int(r.get("down", 0) or 0)
        clicks = int(r.get("clicks", 0) or 0)
        total = up + down
        up_rate = (100 * up / total) if total else 0.0
        tbody.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('fixture') or '—'))}</td>"
            f"<td>{html.escape(str(r.get('hook_type') or '—'))}</td>"
            f"<td>{html.escape(str(r.get('bet_type') or '—'))}</td>"
            f"<td>{html.escape(str(r.get('storyline') or '—'))}</td>"
            f"<td style='text-align:right;'>{up}</td>"
            f"<td style='text-align:right;'>{down}</td>"
            f"<td style='text-align:right;'>{total}</td>"
            f"<td style='text-align:right;'>{up_rate:.0f}%</td>"
            f"<td style='text-align:right;'>{clicks}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Pulse · Reactions</title>
<style>
  body {{ font: 13px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
         background:#0a0a0f; color:#e5e9f2; padding:24px; margin:0; }}
  h1 {{ font-size:18px; margin:0 0 12px; }}
  .summary {{ color:#8b93a7; margin-bottom:16px; font-size:12px; }}
  table {{ border-collapse:collapse; width:100%; max-width:1100px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #1a1f2e;
           text-align:left; vertical-align:top; }}
  th {{ color:#8b93a7; font-weight:600; text-transform:uppercase;
       font-size:10px; letter-spacing:0.5px; position:sticky; top:0;
       background:#0a0a0f; }}
  tr:hover td {{ background:#11141c; }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  .empty {{ color:#8b93a7; padding:24px; text-align:center; }}
</style></head>
<body>
  <h1>Card reactions</h1>
  <div class="summary">
    {total_rows} cards rated ·
    {total_up} thumbs up ·
    {total_down} thumbs down ·
    overall {overall_up_rate:.0f}% up ·
    {total_clicks} CTA clicks
  </div>
  <table>
    <thead><tr>
      <th>Fixture</th><th>Hook</th><th>Bet type</th><th>Storyline</th>
      <th style="text-align:right;">Up</th>
      <th style="text-align:right;">Down</th>
      <th style="text-align:right;">Total</th>
      <th style="text-align:right;">Up rate</th>
      <th style="text-align:right;">Clicks</th>
    </tr></thead>
    <tbody>
      {''.join(tbody) if tbody else '<tr><td colspan="9" class="empty">No reactions yet.</td></tr>'}
    </tbody>
  </table>
</body></html>"""
