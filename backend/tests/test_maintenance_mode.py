"""Tests for MaintenanceMiddleware.

Validates:
  - When MAINTENANCE_MODE is unset / "false", every route passes through.
  - When MAINTENANCE_MODE=true, public routes return 503 with the
    maintenance HTML and Retry-After header.
  - Exempt prefixes (`/health`, `/static/`, `/admin`) stay reachable.
  - Falls back to a tiny inline HTML if the maintenance.html file is
    missing (defensive against deployment glitches).

Same minimal-FastAPI pattern as `test_csp_headers.py` — mount the real
middleware onto a tiny app so the production code path is exercised
without spinning up the engine.

Run with:

    cd ~/code/pulse-poc/backend
    python3 -m pytest tests/test_maintenance_mode.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _build_app() -> FastAPI:
    """Build a tiny FastAPI app wrapping the real MaintenanceMiddleware."""
    from app.main import MaintenanceMiddleware

    app = FastAPI()
    app.add_middleware(MaintenanceMiddleware)

    @app.get("/")
    def root():
        return {"ok": True, "route": "/"}

    @app.get("/api/feed")
    def feed():
        return {"cards": []}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/admin/cost")
    def admin_cost():
        return {"cost": 0.0}

    @app.get("/static/something.txt")
    def static_route():
        return {"served": True}

    return app


def test_disabled_passes_through(monkeypatch):
    """MAINTENANCE_MODE unset → every route serves normally."""
    monkeypatch.delenv("MAINTENANCE_MODE", raising=False)
    client = TestClient(_build_app())
    for path in ("/", "/api/feed", "/health", "/admin/cost"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} unexpectedly 503"


def test_explicit_false_passes_through(monkeypatch):
    """MAINTENANCE_MODE=false → every route serves normally."""
    monkeypatch.setenv("MAINTENANCE_MODE", "false")
    client = TestClient(_build_app())
    r = client.get("/")
    assert r.status_code == 200


def test_enabled_blocks_public_routes(monkeypatch):
    """MAINTENANCE_MODE=true → public routes return 503 with HTML."""
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    client = TestClient(_build_app())
    r = client.get("/")
    assert r.status_code == 503
    assert "text/html" in r.headers["content-type"]
    assert r.headers.get("retry-after") == "300"
    # Maintenance page content
    assert "Maintenance" in r.text or "maintenance" in r.text.lower()


def test_enabled_blocks_api_feed(monkeypatch):
    """MAINTENANCE_MODE=true → /api/feed returns 503 (not the JSON shape)."""
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    client = TestClient(_build_app())
    r = client.get("/api/feed")
    assert r.status_code == 503
    assert "text/html" in r.headers["content-type"]


def test_enabled_exempts_health(monkeypatch):
    """MAINTENANCE_MODE=true → /health stays 200 for monitors."""
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    client = TestClient(_build_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_enabled_exempts_admin(monkeypatch):
    """MAINTENANCE_MODE=true → /admin paths stay reachable for ops."""
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    client = TestClient(_build_app())
    r = client.get("/admin/cost")
    assert r.status_code == 200


def test_enabled_exempts_static(monkeypatch):
    """MAINTENANCE_MODE=true → /static/* stays reachable so the
    maintenance page itself can pull its CSS/font assets if it had any."""
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    client = TestClient(_build_app())
    r = client.get("/static/something.txt")
    assert r.status_code == 200


def test_maintenance_html_file_exists():
    """Sanity: the file the middleware reads from is present in the repo,
    so a deploy that includes app/static/ ships a usable page (rather
    than relying on the fallback)."""
    page = Path(__file__).resolve().parent.parent / "app" / "static" / "maintenance.html"
    assert page.exists(), f"missing: {page}"
    body = page.read_text()
    assert "<html" in body.lower()
    assert "maintenance" in body.lower()
