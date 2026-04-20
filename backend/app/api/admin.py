"""Admin routes — read-only candidate review.

Single page at /admin/candidates that renders a table from the CandidateStore.
No approve/reject actions for v1; that's a later pass once we've watched the
engine run for a while and have a labelling plan.
"""
from __future__ import annotations

import html
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.models.news import CandidateStatus, HookType
from app.services.candidate_store import CandidateStore
from app.services.market_catalog import MarketCatalog
from app.services.game_simulator import GameSimulator


def create_admin_routes(
    store: CandidateStore,
    catalog: MarketCatalog,
    simulator: GameSimulator,
) -> APIRouter:
    router = APIRouter(prefix="/admin")

    @router.get("/candidates", response_class=HTMLResponse)
    async def candidates_page(
        request: Request,
        hook_type: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        game_id: Optional[str] = Query(None),
        above_threshold_only: bool = Query(False),
        limit: int = Query(200, ge=1, le=1000),
    ):
        rows = await store.list_candidates(
            status=status or None,
            hook_type=hook_type or None,
            game_id=game_id or None,
            above_threshold_only=above_threshold_only,
            limit=limit,
        )
        counts = await store.counts_by_hook_and_status()

        body = await _render_page(
            rows=rows,
            counts=counts,
            catalog=catalog,
            simulator=simulator,
            store=store,
            filters={
                "hook_type": hook_type or "",
                "status": status or "",
                "game_id": game_id or "",
                "above_threshold_only": above_threshold_only,
                "limit": limit,
            },
        )
        return HTMLResponse(body)

    @router.get("/candidates.json")
    async def candidates_json(
        hook_type: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        game_id: Optional[str] = Query(None),
        above_threshold_only: bool = Query(False),
        limit: int = Query(200, ge=1, le=1000),
    ):
        rows = await store.list_candidates(
            status=status or None,
            hook_type=hook_type or None,
            game_id=game_id or None,
            above_threshold_only=above_threshold_only,
            limit=limit,
        )
        return JSONResponse({"candidates": [r.model_dump() for r in rows]})

    return router


async def _render_page(
    *,
    rows,
    counts,
    catalog: MarketCatalog,
    simulator: GameSimulator,
    store: CandidateStore,
    filters: dict,
) -> str:
    games = simulator._games
    total = len(rows)
    published = sum(1 for r in rows if r.threshold_passed)

    # Pull news items referenced by these candidates for headline display
    news_ids = {r.news_item_id for r in rows if r.news_item_id}
    news_by_id = {}
    for nid in news_ids:
        n = await store.get_news_item(nid)
        if n:
            news_by_id[nid] = n

    body_rows = []
    for r in rows:
        game = games.get(r.game_id)
        teams = (
            f"{game.home_team.short_name} vs {game.away_team.short_name}"
            if game else r.game_id[:16]
        )
        league = game.broadcast if game else ""
        market_id = r.market_ids[0] if r.market_ids else ""
        market = catalog.get(market_id) if market_id else None
        market_label = market.label if market else "—"
        odds = " / ".join(s.odds or "—" for s in (market.selections[:3] if market else []))

        news = news_by_id.get(r.news_item_id or "")
        headline = news.headline if news else r.narrative

        threshold_mark = "✓" if r.threshold_passed else "✗"

        body_rows.append(
            "<tr>"
            f"<td class='ts'>{_fmt_ts(r.created_at)}</td>"
            f"<td class='hook hook-{html.escape(r.hook_type.value)}'>{html.escape(r.hook_type.value)}</td>"
            f"<td class='teams'>{html.escape(teams)}<div class='league'>{html.escape(league or '')}</div></td>"
            f"<td>{html.escape(market_label)}<div class='odds'>{html.escape(odds)}</div></td>"
            f"<td class='score'>{r.score:.2f}</td>"
            f"<td class='threshold'>{threshold_mark}</td>"
            f"<td class='status status-{html.escape(r.status.value)}'>{html.escape(r.status.value)}</td>"
            f"<td class='headline'>{html.escape(headline or '')[:160]}</td>"
            f"<td class='reason'>{html.escape(r.reason)[:160]}</td>"
            "</tr>"
        )

    table_html = "".join(body_rows) or "<tr><td colspan='9' class='empty'>No candidates yet. Trigger an engine run.</td></tr>"

    # Filter controls
    hook_options = ["", *[h.value for h in HookType]]
    status_options = ["", *[s.value for s in CandidateStatus]]
    hook_select = _select("hook_type", hook_options, filters["hook_type"])
    status_select = _select("status", status_options, filters["status"])
    checked = "checked" if filters["above_threshold_only"] else ""

    # Counts summary
    counts_tbl = _counts_html(counts)

    return _PAGE_TEMPLATE.format(
        total=total,
        published=published,
        rows_html=table_html,
        hook_select=hook_select,
        status_select=status_select,
        above_checked=checked,
        limit=filters["limit"],
        counts_html=counts_tbl,
    )


def _fmt_ts(ts: float) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %H:%M")
    except Exception:
        return ""


def _select(name: str, options: list[str], selected: str) -> str:
    opts = []
    for v in options:
        label = v if v else "(any)"
        sel = " selected" if v == selected else ""
        opts.append(f"<option value='{html.escape(v)}'{sel}>{html.escape(label)}</option>")
    return f"<select name='{name}'>{''.join(opts)}</select>"


def _counts_html(counts: list[dict]) -> str:
    if not counts:
        return "<p class='empty'>No data yet.</p>"
    buckets: dict = {}
    for row in counts:
        buckets.setdefault(row["hook_type"], {})[row["status"]] = row["n"]
    statuses = ["draft", "queued", "published", "rejected", "expired"]
    headers = "<tr><th>Hook</th>" + "".join(f"<th>{s}</th>" for s in statuses) + "<th>Total</th></tr>"
    body = []
    for hook, by_status in sorted(buckets.items()):
        cells = []
        total = 0
        for s in statuses:
            n = by_status.get(s, 0)
            total += n
            cells.append(f"<td>{n}</td>")
        body.append(f"<tr><td>{html.escape(hook)}</td>{''.join(cells)}<td class='total'>{total}</td></tr>")
    return f"<table class='counts'>{headers}{''.join(body)}</table>"


_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>Pulse — Candidates</title>
<style>
  :root {{
    --bg: #0c111d; --panel: #131a2a; --border: #1f2a44;
    --text: #e5e9f2; --muted: #7f8aa8;
    --accent: #7c5cff; --green: #3ddc97; --orange: #f59e0b; --red: #ef4444;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); font-size: 13px; }}
  header {{ padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 24px; background: var(--panel); }}
  h1 {{ margin: 0; font-size: 18px; letter-spacing: 0.06em; color: var(--accent); }}
  .stat {{ color: var(--muted); }} .stat strong {{ color: var(--text); }}
  main {{ padding: 16px 24px; }}
  form.filters {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px;
    flex-wrap: wrap; }}
  form.filters label {{ color: var(--muted); display: flex; gap: 6px; align-items: center; }}
  select, input[type=number] {{ background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 12px; }}
  button {{ background: var(--accent); color: white; border: 0; border-radius: 4px;
    padding: 6px 12px; cursor: pointer; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
  th {{ text-align: left; font-weight: 600; padding: 8px 10px; background: #1a223a;
    color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  td {{ padding: 8px 10px; border-top: 1px solid var(--border); vertical-align: top; }}
  td.empty {{ text-align: center; color: var(--muted); padding: 40px 10px; }}
  td.ts {{ color: var(--muted); white-space: nowrap; }}
  td.hook {{ font-weight: 600; white-space: nowrap; }}
  .hook-injury {{ color: var(--red); }} .hook-team_news {{ color: var(--green); }}
  .hook-transfer {{ color: var(--orange); }} .hook-manager_quote {{ color: var(--muted); }}
  .hook-tactical {{ color: #60a5fa; }} .hook-preview {{ color: var(--muted); }}
  .hook-article {{ color: var(--muted); }}
  .league {{ color: var(--muted); font-size: 11px; margin-top: 2px; }}
  .odds {{ color: var(--muted); font-size: 11px; margin-top: 2px; }}
  td.score {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  td.threshold {{ text-align: center; font-weight: 700; }}
  .status-published {{ color: var(--green); }} .status-queued {{ color: var(--orange); }}
  .status-rejected {{ color: var(--red); }} .status-draft {{ color: var(--muted); }}
  td.headline {{ max-width: 360px; }} td.reason {{ max-width: 320px; color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; }}
  .counts {{ margin-top: 24px; max-width: 720px; }}
  .counts td.total {{ font-weight: 700; }}
</style>
</head>
<body>
<header>
  <h1>PULSE · CANDIDATES</h1>
  <span class='stat'><strong>{total}</strong> in view · <strong>{published}</strong> above threshold</span>
</header>
<main>
  <form class='filters' method='get'>
    <label>hook {hook_select}</label>
    <label>status {status_select}</label>
    <label><input type='checkbox' name='above_threshold_only' value='true' {above_checked}> above threshold only</label>
    <label>limit <input type='number' name='limit' value='{limit}' min='1' max='1000' style='width:80px'></label>
    <button type='submit'>Apply</button>
  </form>
  <table>
    <thead><tr>
      <th>Created</th><th>Hook</th><th>Fixture</th><th>Market</th>
      <th>Score</th><th>≥thr</th><th>Status</th><th>Headline</th><th>Reason</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h3 style='color:var(--muted);font-weight:600;margin-top:32px;'>Counts by hook × status</h3>
  {counts_html}
</main>
</body>
</html>
"""
