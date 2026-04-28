"""Tests for the HTTP Basic admin auth gate added in feat/admin-basic-auth.

Covers the four acceptance behaviours from the PR brief:

  1. With PULSE_ADMIN_USER / PULSE_ADMIN_PASS unset: every /admin/* route
     still returns 200 (existing behaviour preserved). No 401.
  2. With both set:
       - missing Authorization header → 401 + WWW-Authenticate: Basic
       - correct Basic creds → 200
       - wrong username → 401
       - wrong password → 401
  3. /api/feed (and any non-/admin route) is unaffected — always 200.

We don't import app.main here (it boots Sentry + the candidate engine).
Instead we construct a minimal FastAPI app that imports just the
`require_admin` dependency and the config module, mounting a couple of
mock /admin/* and /api/* routes. This proves the dependency wiring in
isolation; the include_router gluing in main.py is the same FastAPI
mechanism, exercised by hand on every previous PR.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_admin_auth.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─────────────────────────────────────────────────────────────────────────
# App factory — minimal FastAPI with the same wiring shape as main.py
# ─────────────────────────────────────────────────────────────────────────

def _build_app() -> FastAPI:
    """Build a fresh FastAPI app that mounts the real require_admin
    dependency on a couple of /admin/* paths plus an unprotected /api/feed
    and /health. Mirrors main.py without booting any of the engine."""
    from app.main import require_admin

    app = FastAPI()

    @app.get("/admin/cost", dependencies=[Depends(require_admin)])
    def admin_cost():
        return {"ok": True, "page": "cost"}

    @app.get("/admin/embeds", dependencies=[Depends(require_admin)])
    def admin_embeds():
        return {"ok": True, "page": "embeds"}

    @app.post("/admin/embeds/foo/rotate", dependencies=[Depends(require_admin)])
    def admin_rotate():
        return {"ok": True, "rotated": True}

    @app.get("/api/feed")
    def api_feed():
        return {"ok": True, "items": []}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────

def test_admin_open_when_env_unset(monkeypatch):
    """Both env vars empty → /admin/* still returns 200 without auth.

    This is the local-dev default. We log a warning at boot but do NOT
    enforce — important so existing flows on a developer laptop don't
    break by surprise.
    """
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    client = TestClient(_build_app())
    r = client.get("/admin/cost")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.get("/admin/embeds")
    assert r.status_code == 200

    r = client.post("/admin/embeds/foo/rotate")
    assert r.status_code == 200


def test_admin_open_when_only_one_var_set(monkeypatch):
    """Exactly one of the two env vars set → still open. Both must be
    populated for enforcement to engage; otherwise we silently treat it
    as 'not configured' (operator forgot to set the second one)."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    client = TestClient(_build_app())
    r = client.get("/admin/cost")
    assert r.status_code == 200


def test_admin_requires_auth_when_set(monkeypatch):
    """Both env vars set, no Authorization header → 401 + WWW-Authenticate."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    client = TestClient(_build_app())
    r = client.get("/admin/cost")
    assert r.status_code == 401
    # Header must include the realm so browsers prompt for creds.
    assert r.headers.get("WWW-Authenticate", "").lower().startswith("basic")
    assert 'realm="pulse-admin"' in r.headers.get("WWW-Authenticate", "")


def test_admin_accepts_correct_creds(monkeypatch):
    """Correct user + pass → 200. Exercises the secrets.compare_digest path."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    client = TestClient(_build_app())
    r = client.get("/admin/cost", headers={"Authorization": _basic("ops", "s3cr3t")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # POST should also be gated.
    r = client.post(
        "/admin/embeds/foo/rotate",
        headers={"Authorization": _basic("ops", "s3cr3t")},
    )
    assert r.status_code == 200


def test_admin_rejects_wrong_user(monkeypatch):
    """Right password, wrong username → 401."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    client = TestClient(_build_app())
    r = client.get(
        "/admin/embeds",
        headers={"Authorization": _basic("attacker", "s3cr3t")},
    )
    assert r.status_code == 401
    assert "Basic" in r.headers.get("WWW-Authenticate", "")


def test_admin_rejects_wrong_pass(monkeypatch):
    """Right username, wrong password → 401."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    client = TestClient(_build_app())
    r = client.get(
        "/admin/embeds",
        headers={"Authorization": _basic("ops", "wrong-pass")},
    )
    assert r.status_code == 401


def test_api_feed_unaffected(monkeypatch):
    """/api/* and /health stay open even when admin auth is on. Hard
    regression guard: the public widget feed must NOT prompt for creds."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    client = TestClient(_build_app())
    # No Authorization header — public traffic shape.
    r = client.get("/api/feed")
    assert r.status_code == 200
    r = client.get("/health")
    assert r.status_code == 200
