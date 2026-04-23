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
        return HTMLResponse(_render_admin_html(rows))

    return router


def _render_admin_html(rows: list[dict[str, Any]]) -> str:
    """Simple admin page, sorted by total desc.

    Columns: fixture, hook_type, bet_type, storyline, up, down, total, up_rate.
    No auth (POC); same posture as /admin/candidates.
    """
    total_rows = len(rows)
    total_up = sum(r.get("up", 0) for r in rows)
    total_down = sum(r.get("down", 0) for r in rows)
    total_all = total_up + total_down
    overall_up_rate = (100 * total_up / total_all) if total_all else 0.0

    tbody = []
    for r in rows:
        up = int(r.get("up", 0) or 0)
        down = int(r.get("down", 0) or 0)
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
    overall {overall_up_rate:.0f}% up
  </div>
  <table>
    <thead><tr>
      <th>Fixture</th><th>Hook</th><th>Bet type</th><th>Storyline</th>
      <th style="text-align:right;">Up</th>
      <th style="text-align:right;">Down</th>
      <th style="text-align:right;">Total</th>
      <th style="text-align:right;">Up rate</th>
    </tr></thead>
    <tbody>
      {''.join(tbody) if tbody else '<tr><td colspan="8" class="empty">No reactions yet.</td></tr>'}
    </tbody>
  </table>
</body></html>"""
