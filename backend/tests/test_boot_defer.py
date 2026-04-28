"""Tests for PULSE_BOOT_DEFER_SECONDS — the redeploy-cost knob added in
feat/boot-defer-tier-cycles.

Cost context: every tier loop's first cycle (HOT 90s, WARM 180s, COOL
270s, COLD 360s after boot) re-pays Haiku + web_search work that the DB
cache may already cover. On a day with multiple redeploys (5 deploys ×
~$0.30 of WARM scout = $1.50 of avoidable spend) we want a single env
knob that pushes the first-cycle floor out so the cache is hit instead.

The fix is a pure helper, `_compute_initial_tier_delay`, that takes
max(offset_for_tier, boot_defer_seconds). When boot_defer is unset (= 0)
the historical stagger is preserved exactly. When it's higher than the
tier's offset the floor wins.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_boot_defer.py -v
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Match the convention used in other backend tests so this file runs
# standalone too.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helper-level tests (no FastAPI boot, no env mutation) ──────────────

def _helper():
    """Import lazily so we don't drag in app.main's global side effects
    at module-collection time."""
    from app.main import _compute_initial_tier_delay
    return _compute_initial_tier_delay


def test_unset_var_preserves_historical_offsets():
    """Case A — boot_defer = 0 → first cycle of each tier matches the
    pre-PR stagger (90 / 180 / 270 / 360s)."""
    fn = _helper()
    assert fn("HOT", 0) == 90
    assert fn("WARM", 0) == 180
    assert fn("COOL", 0) == 270
    assert fn("COLD", 0) == 360


def test_var_higher_than_offset_wins_for_hot():
    """Case B — boot_defer = 1800 → HOT defers a full 30 min instead
    of 90s. Same logic applies to every tier."""
    fn = _helper()
    assert fn("HOT", 1800) == 1800
    assert fn("WARM", 1800) == 1800
    assert fn("COOL", 1800) == 1800
    assert fn("COLD", 1800) == 1800


def test_var_lower_than_offset_loses_max_wins():
    """Case C — boot_defer = 30 (less than HOT's 90s offset) → 90s wins.
    Floor never lowers the existing stagger."""
    fn = _helper()
    assert fn("HOT", 30) == 90
    assert fn("WARM", 30) == 180


def test_unknown_tier_falls_back_to_60s_default():
    """Defensive — a typo'd tier label still gets the 60s fallback,
    and boot_defer still composes via max()."""
    fn = _helper()
    assert fn("BOGUS", 0) == 60
    assert fn("BOGUS", 120) == 120


# ── Config-parser tests (env-var malformed-value safety) ───────────────

def _reload_config_with_env(monkeypatch, value):
    """Reload app.config with PULSE_BOOT_DEFER_SECONDS set (or unset if
    value is None) and return the freshly parsed module-level constant."""
    if value is None:
        monkeypatch.delenv("PULSE_BOOT_DEFER_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PULSE_BOOT_DEFER_SECONDS", value)
    from app import config as _cfg
    importlib.reload(_cfg)
    return _cfg.PULSE_BOOT_DEFER_SECONDS


def test_config_unset_defaults_to_zero(monkeypatch):
    assert _reload_config_with_env(monkeypatch, None) == 0


def test_config_numeric_value_parses(monkeypatch):
    assert _reload_config_with_env(monkeypatch, "1800") == 1800


def test_config_malformed_value_falls_back_to_zero(monkeypatch):
    """Case D — a malformed value like 'abc' must NOT crash boot. It
    falls back to 0, matching every other defensive knob in this file."""
    assert _reload_config_with_env(monkeypatch, "abc") == 0


def test_config_empty_string_falls_back_to_zero(monkeypatch):
    assert _reload_config_with_env(monkeypatch, "") == 0


@pytest.fixture(autouse=True)
def _restore_config_module():
    """Ensure other tests in the suite see the unmutated module after
    we've reloaded it with monkeypatched env."""
    yield
    from app import config as _cfg
    importlib.reload(_cfg)
