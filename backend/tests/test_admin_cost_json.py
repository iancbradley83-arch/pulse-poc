"""Tests for /admin/cost.json (basic + ?detail=1) added in feat/admin-cost-json.

Endpoint contract (item 2 from
``docs/follow-ups-from-ops-session-2026-04-28.md``):

  - Basic call (no query / falsy detail) → top-level fields only:
    ``total_usd``, ``total_calls``, ``limit_usd``, ``days``. Mirrors
    what the /admin/cost HTML page already exposes.
  - ``?detail=1`` → also includes ``by_kind``,
    ``cards_in_feed_now``, ``unique_cards_published_today``,
    ``republish_events_today``, ``rewrite_cache_hits_today``.
  - Auth: gated by the same ``require_admin`` dependency as every
    other /admin/* route. With both env vars set, missing creds → 401.

We follow the pattern from tests/test_admin_auth.py: build a
minimal FastAPI app that mounts the real handler + dependency, with
``feed`` and ``candidate_store`` monkeypatched to deterministic
fixtures so we don't have to boot the live engine.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_admin_cost_json.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─────────────────────────────────────────────────────────────────────────
# App factory — minimal FastAPI mounting the real handler
# ─────────────────────────────────────────────────────────────────────────

def _build_app(monkeypatch, *,
               total_usd: float = 2.68,
               calls: int = 52,
               budget_usd: float = 3.0,
               history: list | None = None,
               by_kind: dict | None = None,
               cards_in_feed: int = 11,
               unique_published: int = 13) -> FastAPI:
    """Stub feed + candidate_store on the imported main module, then
    mount the real /admin/cost.json handler under a fresh FastAPI
    instance. We don't include the rest of main's routers — keeps the
    test fast and isolates the assertion to this endpoint.
    """
    from app import main as _main

    snap = {
        "day_utc": "2026-04-28",
        "total_usd": total_usd,
        "budget_usd": budget_usd,
        "remaining_usd": max(0.0, budget_usd - total_usd),
        "calls": calls,
        "percent_used": (total_usd / budget_usd * 100.0) if budget_usd else 0.0,
    }

    fake_tracker = SimpleNamespace(snapshot=AsyncMock(return_value=snap))
    monkeypatch.setattr(_main, "_get_cost_tracker", lambda: fake_tracker)

    fake_feed = SimpleNamespace(prematch_cards=[object()] * int(cards_in_feed))
    monkeypatch.setattr(_main, "feed", fake_feed)

    fake_store = SimpleNamespace(
        get_daily_cost_history=AsyncMock(return_value=list(history or [])),
        get_daily_cost_by_kind=AsyncMock(return_value=dict(by_kind or {})),
        count_unique_published_cards_since=AsyncMock(return_value=int(unique_published)),
    )
    monkeypatch.setattr(_main, "candidate_store", fake_store)

    app = FastAPI()
    app.add_api_route(
        "/admin/cost.json",
        _main.admin_cost_json,
        methods=["GET"],
        dependencies=[Depends(_main.require_admin)],
    )
    return app


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────
# 1) Basic shape (no detail)
# ─────────────────────────────────────────────────────────────────────────

def test_basic_shape_no_detail(monkeypatch):
    """No query string → top-level fields only, no detail block."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    history = [
        {"date": "2026-04-28", "accumulated_usd": 2.68, "calls": 52},
        {"date": "2026-04-27", "accumulated_usd": 0.83, "calls": 43},
    ]
    app = _build_app(monkeypatch, history=history)
    client = TestClient(app)

    r = client.get("/admin/cost.json")
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level present.
    assert body["total_usd"] == pytest.approx(2.68, rel=1e-6)
    assert body["total_calls"] == 52
    assert body["limit_usd"] == pytest.approx(3.0, rel=1e-6)
    assert isinstance(body["days"], list)
    assert body["days"][0]["date"] == "2026-04-28"
    assert body["days"][0]["usd"] == pytest.approx(2.68, rel=1e-6)
    assert body["days"][0]["calls"] == 52
    assert body["days"][0]["limit_usd"] == pytest.approx(3.0, rel=1e-6)

    # Detail keys absent.
    for k in ("by_kind", "cards_in_feed_now", "unique_cards_published_today",
             "republish_events_today", "rewrite_cache_hits_today"):
        assert k not in body, f"{k!r} should NOT appear without ?detail=1"


# ─────────────────────────────────────────────────────────────────────────
# 2) Detail shape
# ─────────────────────────────────────────────────────────────────────────

def test_detail_shape(monkeypatch):
    """`?detail=1` → basic shape + detail block (by_kind, etc)."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    by_kind = {
        "news_scout": {"usd": 0.57, "calls": 6},
        "boot_scout": {"usd": 2.11, "calls": 46},
    }
    app = _build_app(
        monkeypatch,
        history=[{"date": "2026-04-28", "accumulated_usd": 2.68, "calls": 52}],
        by_kind=by_kind,
        cards_in_feed=11,
        unique_published=13,
    )
    client = TestClient(app)

    r = client.get("/admin/cost.json?detail=1")
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level still present.
    assert "total_usd" in body and "days" in body

    # Detail block present.
    assert body["by_kind"] == {
        "news_scout": {"usd": 0.57, "calls": 6},
        "boot_scout": {"usd": 2.11, "calls": 46},
    }
    assert body["cards_in_feed_now"] == 11
    assert body["unique_cards_published_today"] == 13
    # Currently-not-surfaced fields are explicit null, not omitted.
    assert "republish_events_today" in body and body["republish_events_today"] is None
    assert "rewrite_cache_hits_today" in body and body["rewrite_cache_hits_today"] is None


def test_empty_by_kind_returns_empty_dict(monkeypatch):
    """No per-kind rows yet → ``by_kind: {}``, NOT null and NOT absent."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    app = _build_app(monkeypatch, by_kind={})
    client = TestClient(app)

    r = client.get("/admin/cost.json?detail=1")
    assert r.status_code == 200
    body = r.json()
    assert "by_kind" in body
    assert body["by_kind"] == {}


# ─────────────────────────────────────────────────────────────────────────
# 3) Truthy / falsy detail variants
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on",
                                  " 1 ", "True"])
def test_detail_truthy_variants(monkeypatch, val):
    """1 / true / yes / on (any case, leading/trailing whitespace) all
    enable the detail block."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    app = _build_app(monkeypatch, by_kind={"news_scout": {"usd": 0.5, "calls": 3}})
    client = TestClient(app)

    r = client.get(f"/admin/cost.json?detail={val}")
    assert r.status_code == 200
    body = r.json()
    assert "by_kind" in body, f"detail={val!r} should enable detail block"
    assert body["by_kind"] == {"news_scout": {"usd": 0.5, "calls": 3}}


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off",
                                  "banana", "", " "])
def test_detail_falsy_variants(monkeypatch, val):
    """0 / false / no / off / unknown / empty all skip the detail block."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    app = _build_app(monkeypatch)
    client = TestClient(app)

    r = client.get(f"/admin/cost.json?detail={val}")
    assert r.status_code == 200
    body = r.json()
    assert "by_kind" not in body, f"detail={val!r} should NOT enable detail"
    assert "cards_in_feed_now" not in body


def test_no_detail_param_skips_block(monkeypatch):
    """Param entirely missing → no detail block."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    app = _build_app(monkeypatch)
    client = TestClient(app)
    r = client.get("/admin/cost.json")
    assert r.status_code == 200
    assert "by_kind" not in r.json()


# ─────────────────────────────────────────────────────────────────────────
# 4) Admin auth gate
# ─────────────────────────────────────────────────────────────────────────

def test_admin_auth_gate(monkeypatch):
    """Both env vars set → no creds = 401, correct creds = 200."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    app = _build_app(monkeypatch)
    client = TestClient(app)

    # No header → 401.
    r = client.get("/admin/cost.json")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").lower().startswith("basic")

    # Right creds → 200.
    r = client.get(
        "/admin/cost.json",
        headers={"Authorization": _basic("ops", "s3cr3t")},
    )
    assert r.status_code == 200
    assert "total_usd" in r.json()

    # Wrong creds → 401.
    r = client.get(
        "/admin/cost.json",
        headers={"Authorization": _basic("ops", "wrong")},
    )
    assert r.status_code == 401


def test_admin_auth_gate_with_detail(monkeypatch):
    """Auth gate also covers the detail variant — same dependency."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "ops")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "s3cr3t")

    app = _build_app(monkeypatch)
    client = TestClient(app)

    r = client.get("/admin/cost.json?detail=1")
    assert r.status_code == 401

    r = client.get(
        "/admin/cost.json?detail=1",
        headers={"Authorization": _basic("ops", "s3cr3t")},
    )
    assert r.status_code == 200
    assert "by_kind" in r.json()


# ─────────────────────────────────────────────────────────────────────────
# 5) Empty telemetry
# ─────────────────────────────────────────────────────────────────────────

def test_empty_telemetry_returns_zeroes(monkeypatch):
    """Fresh DB / no LLM calls yet → numeric fields are 0 (not null),
    and the days list is empty (no rows yet)."""
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_USER", "")
    monkeypatch.setattr(_cfg, "PULSE_ADMIN_PASS", "")

    app = _build_app(
        monkeypatch,
        total_usd=0.0,
        calls=0,
        budget_usd=3.0,
        history=[],
        by_kind={},
        cards_in_feed=0,
        unique_published=0,
    )
    client = TestClient(app)

    r = client.get("/admin/cost.json")
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] == 0.0
    assert body["total_calls"] == 0
    assert body["limit_usd"] == pytest.approx(3.0, rel=1e-6)
    assert body["days"] == []

    # Empty fields stay numeric in detail mode too (zeroes, not nulls).
    r = client.get("/admin/cost.json?detail=1")
    body = r.json()
    assert body["total_usd"] == 0.0
    assert body["total_calls"] == 0
    assert body["cards_in_feed_now"] == 0
    assert body["unique_cards_published_today"] == 0
    assert body["by_kind"] == {}
    # The two unsurfaced fields are explicit null.
    assert body["republish_events_today"] is None
    assert body["rewrite_cache_hits_today"] is None


# ─────────────────────────────────────────────────────────────────────────
# 6) candidate_store helper round-trip (count_unique_published_cards_since)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_count_unique_published_cards_since(tmp_path):
    """Helper used by detail block: counts DISTINCT card_id with
    snapshotted_at >= since_ts. Republishes (same card_id) collapse to
    one row by primary key. Older snapshots are excluded by the
    timestamp filter.
    """
    from app.services.candidate_store import CandidateStore

    db_path = str(tmp_path / "pulse_test.db")
    store = CandidateStore(db_path)
    await store.init()

    # Seed three unique cards "today" (snapshotted_at = now — set by upsert).
    for cid in ("card_a", "card_b", "card_c"):
        await store.upsert_published_card(
            card_id=cid,
            snapshot_json="{}",
            candidate_id=cid,
            expires_at=None,
        )
    # Re-upsert one card (republish from boot scout) — should still
    # collapse to one row by primary key.
    await store.upsert_published_card(
        card_id="card_a",
        snapshot_json="{}",
        candidate_id="card_a",
        expires_at=None,
    )

    # since_ts in the past picks up all three.
    n_all = await store.count_unique_published_cards_since(0.0)
    assert n_all == 3

    # since_ts in the far future picks up zero.
    import time as _t
    n_future = await store.count_unique_published_cards_since(_t.time() + 86400.0)
    assert n_future == 0
