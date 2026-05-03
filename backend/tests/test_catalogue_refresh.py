"""Tests for the periodic catalogue-refresh loop config.

The swap path itself is covered indirectly by `fetch_soccer_snapshot`
tests + the importance scorer tests; this file just locks in the env
parsing so a typo can't ship the loop with a refresh interval of "1
second" or some other foot-gun.

Run with:

    cd ~/code/pulse-poc/backend
    python3 -m pytest tests/test_catalogue_refresh.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def env(monkeypatch):
    """Wipe the relevant envs before each test so we test reading defaults."""
    for name in ("PULSE_CATALOGUE_REFRESH_ENABLED",
                 "PULSE_CATALOGUE_REFRESH_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _stub_rogue_jwt(monkeypatch, value: str = "fake-jwt"):
    """Patch the module-level ROGUE_CONFIG_JWT seen by the helper."""
    import app.main as m
    monkeypatch.setattr(m, "ROGUE_CONFIG_JWT", value, raising=False)


def test_kill_switch_disables_refresh(env):
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env)

    env.setenv("PULSE_CATALOGUE_REFRESH_ENABLED", "false")
    enabled, interval, reason = _read_catalogue_refresh_config()
    assert enabled is False
    assert interval == 0
    assert "PULSE_CATALOGUE_REFRESH_ENABLED" in reason


def test_no_rogue_jwt_disables_refresh(env):
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env, "")

    enabled, interval, reason = _read_catalogue_refresh_config()
    assert enabled is False
    assert "ROGUE_CONFIG_JWT" in reason


def test_default_interval_is_4_hours(env):
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env)

    enabled, interval, _ = _read_catalogue_refresh_config()
    assert enabled is True
    assert interval == 14400  # 4h


def test_interval_overrides_via_env(env):
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env)

    env.setenv("PULSE_CATALOGUE_REFRESH_SECONDS", "3600")
    enabled, interval, _ = _read_catalogue_refresh_config()
    assert enabled is True
    assert interval == 3600


def test_interval_floored_at_900_seconds(env):
    """A typo'd 60s interval should not pound Rogue every minute."""
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env)

    env.setenv("PULSE_CATALOGUE_REFRESH_SECONDS", "60")
    enabled, interval, _ = _read_catalogue_refresh_config()
    assert enabled is True
    assert interval == 900


def test_interval_handles_garbage_env(env):
    """Non-numeric value falls back to default rather than crashing the loop."""
    from app.main import _read_catalogue_refresh_config
    _stub_rogue_jwt(env)

    env.setenv("PULSE_CATALOGUE_REFRESH_SECONDS", "not-a-number")
    enabled, interval, _ = _read_catalogue_refresh_config()
    assert enabled is True
    assert interval == 14400
