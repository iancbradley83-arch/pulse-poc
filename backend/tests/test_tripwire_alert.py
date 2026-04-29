"""Tests for the daily-budget tripwire alert (PR feat/cost-tripwire-alert).

What this file proves:

  1. When `can_spend(...)` first returns False on a UTC day, the alert
     emitter is invoked exactly once.
  2. Repeat rejections within the same UTC day do NOT emit additional
     alerts (dedup keyed on UTC date + dedup_key).
  3. A UTC date roll re-arms the alert; the next-day rejection re-fires.
  4. The engine path never raises if the emitter throws — best-effort.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_tripwire_alert.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import cost_tracker as ct_mod
from app.services import alert_emitter as ae_mod
from app.services.cost_tracker import CostTracker
from app.services.candidate_store import CandidateStore


@pytest.fixture
async def tracker_factory():
    """Build a CostTracker on a temp SQLite DB. Cleans up the file on
    teardown. Each test starts with a fresh AlertEmitter dedup set."""
    created: list[str] = []
    ae_mod.AlertEmitter.reset()

    async def _make(*, budget: float = 3.0) -> CostTracker:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False, prefix="pulse_tripwire_test_"
        )
        tmp.close()
        created.append(tmp.name)
        store = CandidateStore(tmp.name)
        await store.init()
        return CostTracker(store=store, daily_budget_usd=budget)

    yield _make

    ae_mod.AlertEmitter.reset()
    for path in created:
        try:
            os.unlink(path)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────
# 1) Single-day: alert fires exactly once
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_fires_once_on_first_rejection(tracker_factory):
    """First `can_spend → False` of the day fires emit_critical. Repeat
    rejections in the same UTC day do NOT re-fire."""
    tracker = await tracker_factory(budget=3.0)

    with patch.object(ae_mod, "_post_webhook") as post_mock, \
         patch("app.services.cost_tracker.today_utc", return_value="2026-04-28"), \
         patch("app.services.alert_emitter._today_utc", return_value="2026-04-28"):
        # Push spend above the 99% ceiling. record_call must run UNDER
        # the patched today_utc so the daily_cost row lands on the same
        # date that can_spend reads — otherwise after the real-world
        # date rolls past 2026-04-28 the read finds $0 and the rejection
        # never fires (this was the regression that broke this test on
        # 2026-04-29).
        await tracker.record_call(
            model="claude-haiku-4-5", kind="test", cost_usd=2.95,
        )
        # Stub out the actual webhook layer so we can assert on emit
        # calls without doing a network round-trip. We patch the inner
        # _post_webhook because emit_critical is the *function under
        # test* — patching it would silence the very behaviour we want.
        # Three rejections, only one alert.
        for _ in range(3):
            ok = await tracker.can_spend(0.50)
            assert ok is False
        # We don't call _post_webhook unless PULSE_ALERTS_WEBHOOK_URL is
        # set, so route through the dedup set directly:
        assert len(ae_mod.AlertEmitter._seen) == 1
        assert ("2026-04-28", "tripwire-2026-04-28") in ae_mod.AlertEmitter._seen


@pytest.mark.asyncio
async def test_alert_fires_webhook_when_url_set(tracker_factory, monkeypatch):
    """When PULSE_ALERTS_WEBHOOK_URL is set, _post_webhook is invoked
    exactly once for the first rejection of the day."""
    tracker = await tracker_factory(budget=3.0)
    monkeypatch.setenv("PULSE_ALERTS_WEBHOOK_URL", "https://example.test/hook")

    # Patch threading.Thread to run the target inline so the test is
    # deterministic without sleeping or joining.
    started: list[dict] = []

    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    with patch.object(ae_mod, "_post_webhook") as post_mock, \
         patch.object(ae_mod.threading, "Thread", _InlineThread), \
         patch("app.services.cost_tracker.today_utc", return_value="2026-04-28"), \
         patch("app.services.alert_emitter._today_utc", return_value="2026-04-28"):
        # record_call must run under the patched today_utc — see comment
        # in test_alert_fires_once_on_first_rejection.
        await tracker.record_call(
            model="claude-haiku-4-5", kind="test", cost_usd=2.95,
        )
        for _ in range(3):
            await tracker.can_spend(0.50)
        assert post_mock.call_count == 1
        url, payload = post_mock.call_args.args
        assert url == "https://example.test/hook"
        assert payload["level"] == "critical"
        assert payload["project"] == "pulse"
        assert "tripwire" in payload["title"].lower()
        assert "3.00" in payload["body"]  # budget value
        assert "timestamp" in payload


@pytest.mark.asyncio
async def test_alert_silent_when_webhook_unset(tracker_factory, monkeypatch):
    """With PULSE_ALERTS_WEBHOOK_URL unset, the emitter logs only —
    no webhook attempt, no exception."""
    tracker = await tracker_factory(budget=3.0)
    await tracker.record_call(model="claude-haiku-4-5", kind="test", cost_usd=2.95)
    monkeypatch.delenv("PULSE_ALERTS_WEBHOOK_URL", raising=False)

    with patch.object(ae_mod, "_post_webhook") as post_mock:
        ok = await tracker.can_spend(0.50)
        assert ok is False
        post_mock.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# 2) UTC date roll: alert re-arms
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_refires_after_utc_date_roll(tracker_factory):
    """A new UTC date causes the dedup key to change, so the next
    rejection of the new day fires a fresh alert."""
    tracker = await tracker_factory(budget=3.0)

    # Day 1 rejection. Patch both today-fns: cost_tracker uses its own
    # `today_utc`; alert_emitter uses its own `_today_utc` for dedup.
    # record_call must run under the same patches so the daily_cost
    # row lands on the date can_spend reads.
    with patch("app.services.cost_tracker.today_utc", return_value="2026-04-28"), \
         patch("app.services.alert_emitter._today_utc", return_value="2026-04-28"):
        await tracker.record_call(
            model="claude-haiku-4-5", kind="test", cost_usd=2.95,
        )
        await tracker.can_spend(0.50)
        await tracker.can_spend(0.50)  # repeat — no extra alert
    assert len(ae_mod.AlertEmitter._seen) == 1
    assert ("2026-04-28", "tripwire-2026-04-28") in ae_mod.AlertEmitter._seen

    # Day 2 rejection — different UTC date → fresh alert. Note the
    # daily_cost row is keyed on date too, so spend resets to 0 on the
    # new key. Re-prime the new day above budget.
    with patch("app.services.cost_tracker.today_utc", return_value="2026-04-29"), \
         patch("app.services.alert_emitter._today_utc", return_value="2026-04-29"):
        await tracker.record_call(
            model="claude-haiku-4-5", kind="test", cost_usd=2.95,
        )
        await tracker.can_spend(0.50)
    assert len(ae_mod.AlertEmitter._seen) == 2
    assert ("2026-04-29", "tripwire-2026-04-29") in ae_mod.AlertEmitter._seen


# ──────────────────────────────────────────────────────────────────────
# 3) Engine path is exception-safe
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_can_spend_never_raises_when_emitter_throws(tracker_factory):
    """If `emit_critical` somehow raises (it shouldn't, but belt-and-
    brace), the engine path still returns False cleanly."""
    tracker = await tracker_factory(budget=3.0)
    await tracker.record_call(model="claude-haiku-4-5", kind="test", cost_usd=2.95)

    def _boom(*a, **kw):
        raise RuntimeError("simulated emitter blowup")

    # Patch the symbol the test target actually imports — `emit_critical`
    # is imported lazily inside `can_spend` from app.services.alert_emitter.
    with patch.object(ae_mod, "emit_critical", side_effect=_boom):
        ok = await tracker.can_spend(0.50)
    assert ok is False  # rejection still clean despite emitter exception


# ──────────────────────────────────────────────────────────────────────
# 4) AlertEmitter unit checks
# ──────────────────────────────────────────────────────────────────────


def test_emit_critical_dedups_within_same_day():
    """Direct unit test on AlertEmitter — same dedup_key + same UTC date
    only enters _seen once."""
    ae_mod.AlertEmitter.reset()
    with patch("app.services.alert_emitter._today_utc", return_value="2026-04-28"):
        ae_mod.emit_critical("t", "b", dedup_key="k1")
        ae_mod.emit_critical("t", "b", dedup_key="k1")
        ae_mod.emit_critical("t", "b", dedup_key="k1")
    assert ae_mod.AlertEmitter._seen == {("2026-04-28", "k1")}


def test_emit_critical_swallows_post_failures(monkeypatch):
    """A misbehaving webhook must not raise into the caller."""
    ae_mod.AlertEmitter.reset()
    monkeypatch.setenv("PULSE_ALERTS_WEBHOOK_URL", "not-a-real-url-zzz")

    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            # Run inline; _post_webhook itself swallows exceptions.
            self._target(*self._args, **(self._kwargs))

    with patch.object(ae_mod.threading, "Thread", _InlineThread):
        # Should not raise — _post_webhook catches URLError/Exception.
        ae_mod.emit_critical("t", "b", dedup_key="k-fail")
