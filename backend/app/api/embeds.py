"""Admin + middleware machinery for the per-operator embed contract.

Three concerns live here:

  1. `match_origin(host, allowed_origins)` — the host-suffix matcher used
     by the request middleware. Supports leading `*.` wildcard prefixes
     (`*.example.com` matches `sub.example.com` but NOT `example.com`),
     literal host equality, and a localhost shortcut.

  2. `verify_embed_token` — a pure-ASGI middleware that gates GET / HEAD
     on `/api/feed` and `/api/feed/*`. Returns 401 on missing/invalid
     token, 403 on origin mismatch. Defaults to OFF — flip
     PULSE_EMBED_TOKEN_REQUIRED=true on Railway to turn enforcement on
     without redeploying.

  3. `create_embed_admin_routes(...)` — a small read-only-ish admin
     surface mirroring /admin/cost. List + detail pages, plus form
     handlers for create / rotate-token / toggle / soft-delete. No auth,
     same convention as the rest of /admin/*.

The admin pages render inline HTML with the same minimal CSS pattern
as /admin/cost — no template engine, no external assets.
"""
from __future__ import annotations

import html
import logging
import secrets
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services.candidate_store import CandidateStore


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Origin matching
# ─────────────────────────────────────────────────────────────────────────

_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _extract_host(origin_or_referer: str) -> str:
    """Pull just the hostname out of a full URL or a bare host string.

    Tolerates `https://foo.com:443/path`, `foo.com`, and trailing dots.
    Lowercased + stripped. Returns "" on garbage input.
    """
    if not origin_or_referer:
        return ""
    raw = origin_or_referer.strip()
    # urlparse needs a scheme to populate netloc reliably; if missing,
    # treat the whole string as a host.
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").strip().lower()
        except Exception:
            host = ""
    else:
        # bare host or "host:port"
        host = raw.split("/", 1)[0].split(":", 1)[0].strip().lower()
    return host.rstrip(".")


def match_origin(host: str, allowed_origins: list[str]) -> bool:
    """Does `host` match any pattern in `allowed_origins`?

    Rules (in order):
      - empty host → False
      - "localhost" entry whitelists localhost / 127.0.0.1 / ::1 (no
        wildcard interaction)
      - "*.example.com" matches `sub.example.com` and deeper subdomains
        but NOT `example.com` itself (the leading-dot suffix rule)
      - everything else: case-insensitive literal equality
    """
    if not host:
        return False
    h = host.strip().lower().rstrip(".")
    has_localhost = any((p or "").strip().lower() == "localhost" for p in allowed_origins)
    if has_localhost and h in _LOCALHOST_HOSTS:
        return True
    for raw in allowed_origins or []:
        if not raw:
            continue
        pat = raw.strip().lower().rstrip(".")
        if not pat:
            continue
        if pat.startswith("*."):
            suffix = pat[1:]                   # ".example.com"
            if h.endswith(suffix) and h != suffix.lstrip("."):
                return True
            continue
        if h == pat:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────
# ASGI middleware
# ─────────────────────────────────────────────────────────────────────────

class EmbedTokenMiddleware:
    """Pure-ASGI middleware that gates `/api/feed` and `/api/feed/*` on
    a valid embed token + origin allowlist match.

    Only intercepts GET/HEAD on /api/feed paths. Every other route
    (including /admin/*, /reactions, /, /health) passes through
    untouched.

    Kill switch: PULSE_EMBED_TOKEN_REQUIRED defaults to "false". When
    "false" the middleware is a no-op; when "true" it enforces:
      - token is present (?embed_token= query param OR
        X-Pulse-Embed-Token header)
      - token resolves to an embed row with active=1
      - request Origin (or Referer fallback) host matches one of the
        embed's allowed_origins (with localhost shortcut)
    """

    _COVERED_PREFIX = "/api/feed"
    _COVERED_METHODS = {"GET", "HEAD"}

    def __init__(self, app, store: CandidateStore, *, enabled: bool):
        self.app = app
        self.store = store
        self.enabled = enabled
        if not enabled:
            logger.info(
                "[embed] enforcement disabled (PULSE_EMBED_TOKEN_REQUIRED=false)"
            )
        else:
            logger.info(
                "[embed] enforcement ENABLED (PULSE_EMBED_TOKEN_REQUIRED=true)"
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "").upper()
        path = scope.get("path") or ""
        if method not in self._COVERED_METHODS or not (
            path == self._COVERED_PREFIX or path.startswith(self._COVERED_PREFIX + "/")
        ):
            await self.app(scope, receive, send)
            return

        # Extract token: prefer query param, fall back to header.
        token = ""
        qs = (scope.get("query_string") or b"").decode("latin-1", errors="ignore")
        if qs:
            for part in qs.split("&"):
                k, _, v = part.partition("=")
                if k == "embed_token" and v:
                    from urllib.parse import unquote
                    token = unquote(v)
                    break
        if not token:
            for name, value in scope.get("headers", []):
                if name.lower() == b"x-pulse-embed-token":
                    token = value.decode("latin-1", errors="ignore").strip()
                    break

        if not token:
            await _send_json(send, 401, {"detail": "embed_token required"})
            return

        try:
            embed = await self.store.get_embed_by_token(token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[embed] token lookup failed: %s", exc)
            await _send_json(send, 503, {"detail": "embed lookup failed"})
            return

        if embed is None or not embed.active:
            await _send_json(send, 401, {"detail": "embed_token invalid"})
            return

        # Origin / Referer header lookup.
        origin_value = ""
        referer_value = ""
        for name, value in scope.get("headers", []):
            lname = name.lower()
            if lname == b"origin":
                origin_value = value.decode("latin-1", errors="ignore")
            elif lname == b"referer":
                referer_value = value.decode("latin-1", errors="ignore")

        host = _extract_host(origin_value) or _extract_host(referer_value)
        if not host:
            await _send_json(send, 403, {"detail": "origin required"})
            return

        if not match_origin(host, embed.allowed_origins):
            await _send_json(send, 403, {"detail": "origin not allowed"})
            return

        await self.app(scope, receive, send)


async def _send_json(send, status: int, body: dict) -> None:
    import json as _json
    payload = _json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


# ─────────────────────────────────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────────────────────────────────

def create_embed_admin_routes(store: CandidateStore) -> APIRouter:
    """Read + write admin surface at /admin/embeds. Mirrors /admin/cost
    style: minimal inline CSS, no auth, no JS framework."""
    router = APIRouter(prefix="/admin")

    @router.get("/embeds", response_class=HTMLResponse)
    async def embeds_list(request: Request):
        try:
            rows = await store.list_embeds(active_only=False)
        except Exception as exc:
            logger.warning("[embed] list failed: %s", exc)
            rows = []
        flash = request.query_params.get("flash") or ""
        return HTMLResponse(_render_list_page(rows, flash=flash))

    @router.get("/embeds.json")
    async def embeds_list_json():
        rows = await store.list_embeds(active_only=False)
        # Token is full-strength on JSON (admin convenience). Same surface
        # as the HTML detail view, just machine-readable.
        return JSONResponse({"embeds": [r.model_dump() for r in rows]})

    @router.get("/embeds/{slug}", response_class=HTMLResponse)
    async def embeds_detail(slug: str, request: Request):
        embed = await store.get_embed_by_slug(slug)
        if embed is None:
            raise HTTPException(404, f"embed slug {slug!r} not found")
        flash = request.query_params.get("flash") or ""
        return HTMLResponse(_render_detail_page(embed, flash=flash))

    @router.post("/embeds")
    async def embeds_create(request: Request):
        # Hand-parse the urlencoded form body so we don't pull in
        # python-multipart as a new dependency. The POC convention is
        # to keep the wheel-graph minimal; FastAPI's Form(...) would
        # require it.
        raw = await request.body()
        from urllib.parse import parse_qs as _parse_qs
        fields = _parse_qs(raw.decode("utf-8", errors="replace"))
        slug = (fields.get("slug", [""])[0] or "").strip().lower()
        display_name = (fields.get("display_name", [""])[0] or "").strip()
        allowed_origins_raw = fields.get("allowed_origins", [""])[0] or ""
        notes = (fields.get("notes", [""])[0] or "").strip()
        if not slug:
            raise HTTPException(400, "slug required")
        # Reject re-use up front so we get a clean redirect instead of
        # an IntegrityError stack trace.
        existing = await store.get_embed_by_slug(slug)
        if existing is not None:
            return RedirectResponse(
                url=f"/admin/embeds?flash=slug+already+exists:+{slug}",
                status_code=303,
            )
        origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
        try:
            await store.create_embed(
                slug=slug,
                display_name=display_name or slug,
                allowed_origins=origins,
                notes=(notes or None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[embed] create failed: %s", exc)
            return RedirectResponse(
                url=f"/admin/embeds?flash=create+failed:+{html.escape(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(url=f"/admin/embeds/{slug}", status_code=303)

    @router.post("/embeds/{slug}/rotate")
    async def embeds_rotate(slug: str):
        try:
            embed = await store.rotate_embed_token(slug)
        except KeyError:
            raise HTTPException(404, f"embed slug {slug!r} not found")
        # Same one-shot log convention as the seed path: print the new
        # token at INFO once so an operator can copy it from logs. Don't
        # ever print it again.
        logger.info(
            "[embed-rotate] slug=%s new_token=%s (rotate via /admin/embeds/%s/rotate)",
            embed.slug, embed.token, embed.slug,
        )
        return RedirectResponse(
            url=f"/admin/embeds/{slug}?flash=token+rotated", status_code=303,
        )

    @router.post("/embeds/{slug}/toggle")
    async def embeds_toggle(slug: str):
        existing = await store.get_embed_by_slug(slug)
        if existing is None:
            raise HTTPException(404, f"embed slug {slug!r} not found")
        await store.update_embed(slug, active=not existing.active)
        return RedirectResponse(url="/admin/embeds", status_code=303)

    @router.post("/embeds/{slug}/delete")
    async def embeds_delete(slug: str):
        existing = await store.get_embed_by_slug(slug)
        if existing is None:
            raise HTTPException(404, f"embed slug {slug!r} not found")
        await store.soft_delete_embed(slug)
        return RedirectResponse(
            url=f"/admin/embeds?flash=soft-deleted+{slug}", status_code=303,
        )

    return router


# ─────────────────────────────────────────────────────────────────────────
# Seed-on-first-run helper (called from main.py startup)
# ─────────────────────────────────────────────────────────────────────────

async def seed_default_embed_if_empty(store: CandidateStore) -> Optional[str]:
    """If `embeds` is empty, insert the apuesta-total default and return
    the new token (so the caller can log it once at INFO). Idempotent —
    returns None if any row already exists.
    """
    try:
        existing = await store.list_embeds(active_only=False)
    except Exception as exc:
        logger.warning("[embed-seed] could not check existing rows: %s", exc)
        return None
    if existing:
        return None
    token = secrets.token_urlsafe(32)
    try:
        await store.create_embed(
            slug="apuesta-total",
            display_name="Apuesta Total (Peru)",
            allowed_origins=[
                "*.apuestatotal.com.pe",
                "apuestatotal.pe",
                "pulse-poc-production.up.railway.app",
                "localhost",
            ],
            token=token,
            notes="Auto-seeded on first deploy. Rotate before public launch.",
            active=True,
        )
    except Exception as exc:
        logger.warning("[embed-seed] create failed: %s", exc)
        return None
    return token


# ─────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────

def _render_list_page(rows, *, flash: str = "") -> str:
    body_rows = []
    for r in rows:
        active_label = "yes" if r.active else "no"
        active_cls = "active-yes" if r.active else "active-no"
        origins = ", ".join(r.allowed_origins or [])
        token_short = (r.token[:6] + "…") if r.token else "—"
        body_rows.append(
            "<tr>"
            f"<td><a href='/admin/embeds/{html.escape(r.slug)}'>{html.escape(r.slug)}</a></td>"
            f"<td>{html.escape(r.display_name or '')}</td>"
            f"<td class='{active_cls}'>{active_label}</td>"
            f"<td class='origins'>{html.escape(origins)}</td>"
            f"<td class='mono'>{html.escape(token_short)}</td>"
            f"<td class='ts'>{html.escape(r.created_at or '')}</td>"
            f"<td>{html.escape(r.notes or '')}</td>"
            "<td class='actions'>"
            f"  <form method='post' action='/admin/embeds/{html.escape(r.slug)}/toggle' style='display:inline'>"
            f"    <button type='submit'>{'deactivate' if r.active else 'reactivate'}</button>"
            "  </form>"
            f"  <form method='post' action='/admin/embeds/{html.escape(r.slug)}/delete' style='display:inline'>"
            "    <button type='submit'>soft-delete</button>"
            "  </form>"
            "</td>"
            "</tr>"
        )
    rows_html = (
        "".join(body_rows)
        or "<tr><td colspan='8' class='empty'>No embeds yet — create one below.</td></tr>"
    )
    flash_html = (
        f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""
    )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Pulse — embeds</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif;
         max-width: 1100px; margin: 32px auto; padding: 0 16px; color: #222; }}
  h1 {{ margin-bottom: 0.2em; }}
  .sub {{ color: #666; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee;
           vertical-align: top; }}
  th {{ background: #fafafa; }}
  td.empty {{ text-align: center; color: #888; padding: 30px; }}
  .mono {{ font-family: ui-monospace, Menlo, monospace; }}
  .ts {{ color: #666; white-space: nowrap; }}
  .origins {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  .active-yes {{ color: #166534; font-weight: 600; }}
  .active-no  {{ color: #b91c1c; font-weight: 600; }}
  .actions form {{ margin-right: 4px; }}
  .actions button {{ background: #f5f7fa; border: 1px solid #d1d5db; padding: 4px 8px;
                     font-size: 11px; cursor: pointer; border-radius: 4px; }}
  .actions button:hover {{ background: #eef2f7; }}
  .flash {{ background: #fef3c7; border: 1px solid #f59e0b; padding: 10px 14px;
            border-radius: 6px; margin-top: 12px; font-size: 13px; }}
  form.create {{ margin-top: 28px; padding: 16px; background: #f5f7fa;
                 border: 1px solid #e5e7eb; border-radius: 6px; }}
  form.create label {{ display: block; margin: 8px 0; font-size: 13px; }}
  form.create input[type=text] {{ width: 100%; max-width: 480px; padding: 6px 8px;
                                  border: 1px solid #d1d5db; border-radius: 4px;
                                  font-family: inherit; font-size: 13px; }}
  form.create button {{ background: #2563eb; color: white; border: 0;
                        padding: 8px 16px; border-radius: 4px; cursor: pointer;
                        font-size: 13px; margin-top: 8px; }}
</style>
</head>
<body>
<h1>Pulse — embeds</h1>
<p class="sub">Per-operator widget registrations. Token gates /api/feed when
PULSE_EMBED_TOKEN_REQUIRED=true; defaults off so the live widget keeps working.</p>
{flash_html}
<table>
  <thead>
    <tr>
      <th>slug</th><th>display_name</th><th>active</th>
      <th>allowed_origins</th><th>token</th>
      <th>created_at</th><th>notes</th><th>actions</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>

<form class="create" method="post" action="/admin/embeds">
  <h2 style="margin-top:0;font-size:16px;">New embed</h2>
  <label>slug <input type="text" name="slug" required placeholder="apuesta-total"></label>
  <label>display_name <input type="text" name="display_name" required placeholder="Apuesta Total (Peru)"></label>
  <label>allowed_origins (comma-separated)
    <input type="text" name="allowed_origins" required placeholder="*.apuestatotal.com.pe, apuestatotal.pe, localhost">
  </label>
  <label>notes (optional) <input type="text" name="notes"></label>
  <button type="submit">Create embed</button>
</form>
</body></html>
"""


def _render_detail_page(embed, *, flash: str = "") -> str:
    import json as _json
    origins_html = "".join(
        f"<li class='mono'>{html.escape(o)}</li>"
        for o in (embed.allowed_origins or [])
    ) or "<li><em>(none)</em></li>"
    theme_pretty = (
        _json.dumps(embed.theme_overrides, indent=2)
        if embed.theme_overrides else "{}"
    )
    flash_html = (
        f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""
    )
    active_label = "yes" if embed.active else "no (soft-deleted or toggled off)"
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Pulse — embed {html.escape(embed.slug)}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif;
         max-width: 880px; margin: 32px auto; padding: 0 16px; color: #222; }}
  h1 {{ margin-bottom: 0.2em; }}
  .sub {{ color: #666; margin-top: 0; }}
  .row {{ margin: 14px 0; }}
  .row label {{ font-weight: 600; color: #444; display: block;
                font-size: 12px; text-transform: uppercase;
                letter-spacing: 0.04em; margin-bottom: 4px; }}
  .row .v {{ font-family: ui-monospace, Menlo, monospace; font-size: 13px;
             padding: 8px 10px; background: #f5f7fa; border: 1px solid #e5e7eb;
             border-radius: 4px; word-break: break-all; }}
  ul {{ margin: 4px 0 0 20px; padding: 0; }}
  pre {{ background: #f5f7fa; border: 1px solid #e5e7eb; padding: 10px;
        border-radius: 4px; font-size: 12px; }}
  .flash {{ background: #fef3c7; border: 1px solid #f59e0b; padding: 10px 14px;
            border-radius: 6px; margin-top: 12px; font-size: 13px; }}
  form.action {{ display: inline; margin-right: 6px; }}
  form.action button {{ background: #f5f7fa; border: 1px solid #d1d5db;
                        padding: 6px 12px; cursor: pointer; border-radius: 4px;
                        font-size: 13px; }}
  form.action button:hover {{ background: #eef2f7; }}
  form.action.danger button {{ color: #b91c1c; border-color: #fecaca; }}
  a.back {{ color: #2563eb; text-decoration: none; font-size: 13px; }}
</style>
</head>
<body>
<a class="back" href="/admin/embeds">&larr; back to embeds</a>
<h1>Embed: {html.escape(embed.display_name)}</h1>
<p class="sub">slug: <code>{html.escape(embed.slug)}</code></p>
{flash_html}

<div class="row"><label>Token (full)</label>
  <div class="v">{html.escape(embed.token)}</div>
</div>

<div class="row"><label>Active</label>
  <div class="v">{active_label}</div>
</div>

<div class="row"><label>Allowed origins</label>
  <ul>{origins_html}</ul>
</div>

<div class="row"><label>Theme overrides (wave-4 reserved)</label>
  <pre>{html.escape(theme_pretty)}</pre>
</div>

<div class="row"><label>Created at</label>
  <div class="v">{html.escape(embed.created_at or '—')}</div>
</div>

<div class="row"><label>Notes</label>
  <div class="v">{html.escape(embed.notes or '—')}</div>
</div>

<div style="margin-top:24px;">
  <form class="action" method="post" action="/admin/embeds/{html.escape(embed.slug)}/rotate">
    <button type="submit">Rotate token</button>
  </form>
  <form class="action" method="post" action="/admin/embeds/{html.escape(embed.slug)}/toggle">
    <button type="submit">{('Deactivate' if embed.active else 'Reactivate')}</button>
  </form>
  <form class="action danger" method="post" action="/admin/embeds/{html.escape(embed.slug)}/delete">
    <button type="submit">Soft-delete</button>
  </form>
</div>
</body></html>
"""
