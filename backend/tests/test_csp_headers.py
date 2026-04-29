"""Tests for SecurityHeadersMiddleware CSP directive (chore/csp-fix-and-ws-cleanup).

Validates that the production CSP directive built into
SecurityHeadersMiddleware widens style-src / font-src enough to load the
JetBrains Mono brand font from Google Fonts without a CSP violation.

Specifically:
  1. style-src includes `https://fonts.googleapis.com` so the CSS file
     served from `fonts.googleapis.com/css2?family=...` loads.
  2. font-src includes `https://fonts.gstatic.com` (the actual woff2 host)
     and `data:` (for inline fallback fonts).
  3. The header is sent on a real HTTP response (TestClient GET /).

We mount the real `SecurityHeadersMiddleware` class from app.main onto a
minimal FastAPI app, identical pattern to test_gzip.py / test_admin_auth.py.
This avoids spinning up the candidate engine while still exercising the
exact production middleware code path.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_csp_headers.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _build_app() -> FastAPI:
    """Build a tiny FastAPI app wrapped in the real SecurityHeadersMiddleware.

    Lazy import so app.main's module-level side effects don't fire at test
    collection time on machines where they'd be expensive."""
    from app.main import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return app


def test_csp_includes_google_fonts():
    """style-src must include `https://fonts.googleapis.com` so the
    brand-font stylesheet loads in the deployed widget. Without this the
    page falls back to system mono and Playwright reports a CSP violation."""
    client = TestClient(_build_app())
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    assert csp is not None, "Content-Security-Policy header must be set"
    # Pull the style-src directive specifically — checking the whole CSP
    # string would also accept a stray match in a different directive.
    style_src = next(
        (
            d.strip()
            for d in csp.split(";")
            if d.strip().startswith("style-src")
        ),
        None,
    )
    assert style_src is not None, f"style-src missing from CSP: {csp!r}"
    assert "https://fonts.googleapis.com" in style_src, (
        f"style-src must allow fonts.googleapis.com, got: {style_src!r}"
    )


def test_csp_font_src_set():
    """font-src must include `https://fonts.gstatic.com` (woff2 host) and
    `data:` (inline fallback). Required because default-src 'self' alone
    blocks the cross-origin font fetch even when style-src allows the CSS."""
    client = TestClient(_build_app())
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    assert csp is not None, "Content-Security-Policy header must be set"
    font_src = next(
        (
            d.strip()
            for d in csp.split(";")
            if d.strip().startswith("font-src")
        ),
        None,
    )
    assert font_src is not None, f"font-src missing from CSP: {csp!r}"
    assert "https://fonts.gstatic.com" in font_src, (
        f"font-src must allow fonts.gstatic.com, got: {font_src!r}"
    )
    assert "data:" in font_src, (
        f"font-src must allow data: URIs, got: {font_src!r}"
    )
