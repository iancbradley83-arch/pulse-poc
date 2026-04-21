"""Admin routes — read-only candidate review.

Single page at /admin/candidates that renders a table from the CandidateStore.
No approve/reject actions for v1; that's a later pass once we've watched the
engine run for a while and have a labelling plan.
"""
from __future__ import annotations

import html
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.models.news import CandidateStatus, HookType
from app.services.candidate_store import CandidateStore
from app.services.market_catalog import MarketCatalog
from app.services.game_simulator import GameSimulator


ALLOWED_REASON_CODES = {
    "wrong_team",
    "bad_headline",
    "odds_nonsensical",
    "story_unrelated",
    "angle_mismatch",
    "duplicate",
    "other",
}


class ReviewBody(BaseModel):
    verdict: str                      # "good" | "bad"
    reason_code: Optional[str] = None
    note: Optional[str] = None
    reviewer: Optional[str] = None


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
        verdicts = await store.latest_verdict_by_candidate()
        summary = await store.review_summary()

        body = await _render_page(
            rows=rows,
            counts=counts,
            verdicts=verdicts,
            review_summary=summary,
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

    @router.post("/candidates/{candidate_id}/review")
    async def review_candidate(candidate_id: str, body: ReviewBody):
        verdict = (body.verdict or "").lower().strip()
        if verdict not in ("good", "bad"):
            raise HTTPException(400, "verdict must be 'good' or 'bad'")
        reason = (body.reason_code or "").strip().lower() or None
        if reason is not None and reason not in ALLOWED_REASON_CODES:
            raise HTTPException(400, f"reason_code must be one of {sorted(ALLOWED_REASON_CODES)}")
        await store.save_review(
            candidate_id=candidate_id,
            verdict=verdict,
            reason_code=reason,
            note=body.note,
            reviewer=body.reviewer,
        )
        summary = await store.review_summary()
        return {"ok": True, "summary": summary}

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
    verdicts: dict,
    review_summary: dict,
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

        bet_type_tag = r.bet_type.value if hasattr(r.bet_type, "value") else str(r.bet_type)
        is_bb = bet_type_tag == "bet_builder" and len(r.market_ids) >= 2

        # Market cell: for BBs show every leg; for singles show the primary market.
        if is_bb:
            leg_rows = []
            leg_total = 1.0
            leg_total_valid = True
            for mid, sid in zip(r.market_ids, r.selection_ids):
                leg_market = catalog.get(mid)
                leg_sel = None
                if leg_market is not None:
                    leg_sel = next((s for s in leg_market.selections if s.selection_id == sid), None)
                label = leg_sel.label if leg_sel else "?"
                mkt_label = leg_market.label if leg_market else "?"
                odds_s = leg_sel.odds if leg_sel and leg_sel.odds else "—"
                try:
                    leg_total *= float(odds_s)
                except Exception:
                    leg_total_valid = False
                leg_rows.append(
                    f"<div class='bb-leg'>"
                    f"  <span class='leg-market'>{html.escape(mkt_label)}</span>"
                    f"  <span class='leg-label'>{html.escape(label)}</span>"
                    f"  <span class='leg-odds'>{html.escape(str(odds_s))}</span>"
                    f"</div>"
                )
            market_cell = (
                "<div class='bb-legs'>"
                + "".join(leg_rows)
                + f"<div class='bb-total'>× {leg_total:.2f}</div>"
                + "</div>"
            ) if leg_total_valid else (
                "<div class='bb-legs'>" + "".join(leg_rows)
                + "<div class='bb-total'>× ?</div></div>"
            )
        else:
            market_id = r.market_ids[0] if r.market_ids else ""
            market = catalog.get(market_id) if market_id else None
            market_label = market.label if market else "—"
            odds = " / ".join(s.odds or "—" for s in (market.selections[:3] if market else []))
            market_cell = (
                f"{html.escape(market_label)}"
                f"<div class='odds'>{html.escape(odds)}</div>"
            )

        news = news_by_id.get(r.news_item_id or "")
        headline = news.headline if news else r.narrative

        threshold_mark = "✓" if r.threshold_passed else "✗"
        verdict = verdicts.get(r.id)

        review_cell = (
            f"<div class='review' data-cand-id='{html.escape(r.id)}' data-verdict='{html.escape(verdict or '')}'>"
            f"  <button class='thumb good{' on' if verdict == 'good' else ''}' title='Mark good' onclick=\"review('{html.escape(r.id)}','good')\">👍</button>"
            f"  <button class='thumb bad{' on' if verdict == 'bad' else ''}' title='Mark bad' onclick=\"review('{html.escape(r.id)}','bad')\">👎</button>"
            "</div>"
        )

        body_rows.append(
            "<tr>"
            f"<td class='ts'>{_fmt_ts(r.created_at)}</td>"
            f"<td class='hook hook-{html.escape(r.hook_type.value)}'>{html.escape(r.hook_type.value)}</td>"
            f"<td class='bet-type bt-{html.escape(bet_type_tag)}'>{html.escape(bet_type_tag)}</td>"
            f"<td class='teams'>{html.escape(teams)}<div class='league'>{html.escape(league or '')}</div></td>"
            f"<td class='market-cell'>{market_cell}</td>"
            f"<td class='score'>{r.score:.2f}</td>"
            f"<td class='threshold'>{threshold_mark}</td>"
            f"<td class='status status-{html.escape(r.status.value)}'>{html.escape(r.status.value)}</td>"
            f"<td class='headline'>{html.escape(headline or '')[:160]}</td>"
            f"<td class='reason'>{html.escape(r.reason)[:160]}</td>"
            f"<td class='review-cell'>{review_cell}</td>"
            "</tr>"
        )

    table_html = "".join(body_rows) or "<tr><td colspan='11' class='empty'>No candidates yet. Trigger an engine run.</td></tr>"

    # Filter controls
    hook_options = ["", *[h.value for h in HookType]]
    status_options = ["", *[s.value for s in CandidateStatus]]
    hook_select = _select("hook_type", hook_options, filters["hook_type"])
    status_select = _select("status", status_options, filters["status"])
    checked = "checked" if filters["above_threshold_only"] else ""

    # Counts summary
    counts_tbl = _counts_html(counts)

    # Review summary — top-of-page dashboard strip
    rev = review_summary or {"total_reviews": 0, "good": 0, "bad": 0, "bad_pct": 0.0, "top_bad_reasons": []}
    top_reasons = ", ".join(f"{r['reason_code']}×{r['n']}" for r in rev.get("top_bad_reasons", [])[:3]) or "—"

    return _PAGE_TEMPLATE.format(
        total=total,
        published=published,
        rows_html=table_html,
        hook_select=hook_select,
        status_select=status_select,
        above_checked=checked,
        limit=filters["limit"],
        counts_html=counts_tbl,
        reviews_total=rev["total_reviews"],
        reviews_good=rev["good"],
        reviews_bad=rev["bad"],
        reviews_bad_pct=rev["bad_pct"],
        top_reasons=html.escape(top_reasons),
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
    display: flex; align-items: baseline; gap: 24px; background: var(--panel); flex-wrap: wrap; }}
  h1 {{ margin: 0; font-size: 18px; letter-spacing: 0.06em; color: var(--accent); }}
  .stat {{ color: var(--muted); }} .stat strong {{ color: var(--text); }}
  .review-strip {{ display: flex; gap: 18px; margin-left: auto; padding: 6px 10px;
    background: rgba(124,92,255,0.06); border: 1px solid rgba(124,92,255,0.18);
    border-radius: 8px; font-size: 12px; }}
  .review-strip .k {{ color: var(--muted); margin-right: 4px; }}
  .review-strip .v {{ color: var(--text); font-weight: 700; }}
  .review-strip .bad {{ color: var(--red); font-weight: 700; }}
  .review-strip .good {{ color: var(--green); font-weight: 700; }}
  main {{ padding: 16px 24px; }}
  form.filters {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px;
    flex-wrap: wrap; }}
  form.filters label {{ color: var(--muted); display: flex; gap: 6px; align-items: center; }}
  select, input[type=number] {{ background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 12px; }}
  button.apply-btn {{ background: var(--accent); color: white; border: 0; border-radius: 4px;
    padding: 6px 12px; cursor: pointer; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
  th {{ text-align: left; font-weight: 600; padding: 8px 10px; background: #1a223a;
    color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  td {{ padding: 8px 10px; border-top: 1px solid var(--border); vertical-align: top; }}
  td.empty {{ text-align: center; color: var(--muted); padding: 40px 10px; }}
  td.ts {{ color: var(--muted); white-space: nowrap; }}
  td.hook {{ font-weight: 600; white-space: nowrap; }}
  td.bet-type {{ font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; }}
  .bt-bet_builder {{ color: var(--accent); }} .bt-single {{ color: var(--muted); }} .bt-combo {{ color: var(--orange); }}
  .hook-injury {{ color: var(--red); }} .hook-team_news {{ color: var(--green); }}
  .hook-transfer {{ color: var(--orange); }} .hook-manager_quote {{ color: var(--muted); }}
  .hook-tactical {{ color: #60a5fa; }} .hook-preview {{ color: var(--muted); }}
  .hook-article {{ color: var(--muted); }}
  .league {{ color: var(--muted); font-size: 11px; margin-top: 2px; }}
  .odds {{ color: var(--muted); font-size: 11px; margin-top: 2px; }}
  td.market-cell {{ min-width: 260px; }}
  .bb-legs {{ display: flex; flex-direction: column; gap: 3px; }}
  .bb-leg {{ display: grid; grid-template-columns: 80px 1fr 50px; gap: 6px;
    align-items: baseline; font-size: 11px; padding: 3px 0; }}
  .bb-leg .leg-market {{ color: var(--muted); font-size: 9.5px;
    text-transform: uppercase; letter-spacing: 0.04em; }}
  .bb-leg .leg-label {{ color: var(--text); font-weight: 600; }}
  .bb-leg .leg-odds {{ color: var(--text); font-family: ui-monospace, Menlo, monospace;
    font-weight: 700; text-align: right; }}
  .bb-total {{ color: #c6ff3d; font-family: ui-monospace, Menlo, monospace;
    font-weight: 700; font-size: 12px; text-align: right; margin-top: 4px;
    padding-top: 4px; border-top: 1px dashed var(--border); }}
  td.score {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  td.threshold {{ text-align: center; font-weight: 700; }}
  .status-published {{ color: var(--green); }} .status-queued {{ color: var(--orange); }}
  .status-rejected {{ color: var(--red); }} .status-draft {{ color: var(--muted); }}
  td.headline {{ max-width: 360px; }}
  td.reason {{ max-width: 320px; color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; }}
  td.review-cell {{ white-space: nowrap; }}
  .review {{ display: inline-flex; gap: 4px; }}
  .thumb {{ background: transparent; border: 1px solid var(--border); border-radius: 4px;
    padding: 4px 7px; cursor: pointer; font-size: 14px; color: var(--muted);
    transition: background 120ms, color 120ms, border-color 120ms; }}
  .thumb:hover {{ border-color: var(--accent); color: var(--text); }}
  .thumb.good.on {{ background: rgba(61, 220, 151, 0.18); border-color: var(--green); color: var(--green); }}
  .thumb.bad.on {{ background: rgba(239, 68, 68, 0.15); border-color: var(--red); color: var(--red); }}
  .counts {{ margin-top: 24px; max-width: 720px; }}
  .counts td.total {{ font-weight: 700; }}
  .dialog-backdrop {{ position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: none; align-items: center; justify-content: center; z-index: 10; }}
  .dialog-backdrop.open {{ display: flex; }}
  .dialog {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; min-width: 320px; max-width: 440px; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }}
  .dialog h3 {{ margin: 0 0 10px; font-size: 14px; font-weight: 700; color: var(--text); }}
  .dialog label {{ display: flex; align-items: center; gap: 8px; padding: 6px 0;
    color: var(--muted); font-size: 13px; cursor: pointer; }}
  .dialog label:hover {{ color: var(--text); }}
  .dialog input[type=text] {{ width: 100%; background: #0b1121; color: var(--text);
    border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 12px;
    margin-top: 4px; }}
  .dialog .buttons {{ display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }}
  .dialog .buttons button {{ background: transparent; color: var(--muted); border: 1px solid var(--border);
    border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 12px; }}
  .dialog .buttons button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>PULSE · CANDIDATES</h1>
  <span class='stat'><strong>{total}</strong> in view · <strong>{published}</strong> above threshold</span>
  <div class='review-strip'>
    <span><span class='k'>reviewed</span><span class='v'>{reviews_total}</span></span>
    <span><span class='k'>good</span><span class='v good'>{reviews_good}</span></span>
    <span><span class='k'>bad</span><span class='v bad'>{reviews_bad}</span> <span class='k'>({reviews_bad_pct}%)</span></span>
    <span><span class='k'>top bad</span><span class='v'>{top_reasons}</span></span>
  </div>
</header>
<main>
  <form class='filters' method='get'>
    <label>hook {hook_select}</label>
    <label>status {status_select}</label>
    <label><input type='checkbox' name='above_threshold_only' value='true' {above_checked}> above threshold only</label>
    <label>limit <input type='number' name='limit' value='{limit}' min='1' max='1000' style='width:80px'></label>
    <button type='submit' class='apply-btn'>Apply</button>
  </form>
  <table>
    <thead><tr>
      <th>Created</th><th>Hook</th><th>Type</th><th>Fixture</th><th>Market</th>
      <th>Score</th><th>≥thr</th><th>Status</th><th>Headline</th><th>Reason</th><th>Review</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h3 style='color:var(--muted);font-weight:600;margin-top:32px;'>Counts by hook × status</h3>
  {counts_html}
</main>

<div class='dialog-backdrop' id='reason-dialog'>
  <div class='dialog'>
    <h3>Why is this card bad?</h3>
    <form id='reason-form'>
      <label><input type='radio' name='reason' value='wrong_team'> Wrong team attached</label>
      <label><input type='radio' name='reason' value='story_unrelated'> Story isn't about this match</label>
      <label><input type='radio' name='reason' value='bad_headline'> Headline is weak / off</label>
      <label><input type='radio' name='reason' value='angle_mismatch'> Angle doesn't fit the market</label>
      <label><input type='radio' name='reason' value='odds_nonsensical'> Odds / BB combo looks wrong</label>
      <label><input type='radio' name='reason' value='duplicate'> Duplicate of another card</label>
      <label><input type='radio' name='reason' value='other' checked> Other</label>
      <label>Note (optional): <input type='text' name='note' placeholder='free-text detail'></label>
      <div class='buttons'>
        <button type='button' onclick='closeDialog()'>Cancel</button>
        <button type='submit' class='primary'>Save</button>
      </div>
    </form>
  </div>
</div>

<script>
  const _state = {{ pendingId: null }};
  async function review(candId, verdict) {{
    if (verdict === 'bad') {{
      _state.pendingId = candId;
      document.getElementById('reason-dialog').classList.add('open');
      return;
    }}
    await postReview(candId, 'good', null, null);
    location.reload();
  }}
  function closeDialog() {{
    _state.pendingId = null;
    document.getElementById('reason-dialog').classList.remove('open');
  }}
  document.getElementById('reason-form').addEventListener('submit', async (e) => {{
    e.preventDefault();
    const form = e.target;
    const reason = form.reason.value;
    const note = form.note.value || null;
    const id = _state.pendingId;
    closeDialog();
    if (!id) return;
    await postReview(id, 'bad', reason, note);
    location.reload();
  }});
  async function postReview(candId, verdict, reason_code, note) {{
    await fetch('/admin/candidates/' + encodeURIComponent(candId) + '/review', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ verdict, reason_code, note }}),
    }});
  }}
</script>
</body>
</html>
"""
