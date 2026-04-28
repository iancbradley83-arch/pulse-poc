"""Tests for PULSE_BOOT_SCOUT_ENABLED — the redeploy-cost kill switch
added in feat/zero-cost-redeploy.

Cost context: every cold start runs `_run_candidate_engine(...)` once
(the "boot scout"), which pays Haiku + web_search work that the snapshot
rehydrate path (PR #65) already covers from SQLite. With 12+ deploys in
a single day adding ~$0.20-0.30 each, today (2026-04-28) hit $2.78 of
$3.00 budget — 92.7%, almost entirely from boot churn. PR #77 deferred
TIER LOOPS' first cycles via PULSE_BOOT_DEFER_SECONDS, but the on-startup
boot scout is a separate path with its own gate at backend/app/main.py.

The kill switch is a third independent flag. When OFF (default), boot
completes free: catalog + featured BBs + snapshot rehydrate (all free)
run, but `_run_candidate_engine` is skipped. Tier loops still kick in
after PULSE_BOOT_DEFER_SECONDS. Set to "true" if a debugger needs a
fresh scout (demo prep, content reset).

The decision lives in a pure helper, `_should_run_boot_scout`, so tests
can exercise it without booting FastAPI + dragging in `_load_rogue_prematch`.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_boot_scout_kill_switch.py -v
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

# Match the convention used in other backend tests so this file runs
# standalone too.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _helper():
    """Import lazily so we don't drag in app.main's global side effects
    at module-collection time."""
    from app.main import _should_run_boot_scout
    return _should_run_boot_scout


# ── Helper-level tests (no FastAPI boot, no env mutation) ──────────────

def test_boot_scout_off_by_default_skips_engine():
    """Default state — news_ingest + rerun ON, boot_scout OFF (the
    default). Engine should NOT run; log line should announce
    "boot scout disabled"."""
    fn = _helper()
    should_run, msg = fn(
        news_ingest_enabled=True,
        rerun_enabled=True,
        boot_scout_enabled=False,
    )
    assert should_run is False
    assert "boot scout disabled" in msg
    assert "snapshot rehydrate" in msg


def test_boot_scout_on_runs_engine():
    """Explicit override — boot_scout=true → engine runs, log line
    says "boot scout active"."""
    fn = _helper()
    should_run, msg = fn(
        news_ingest_enabled=True,
        rerun_enabled=True,
        boot_scout_enabled=True,
    )
    assert should_run is True
    assert "boot scout active" in msg


def test_news_ingest_pause_overrides_boot_scout_on():
    """The existing kill switch wins. Even if boot_scout=true, when
    news_ingest=false the engine stays paused."""
    fn = _helper()
    should_run, msg = fn(
        news_ingest_enabled=False,
        rerun_enabled=True,
        boot_scout_enabled=True,
    )
    assert should_run is False
    assert "engine paused" in msg
    assert "news_ingest=false" in msg


def test_rerun_disabled_overrides_boot_scout_on():
    """Same priority rule for the rerun kill switch."""
    fn = _helper()
    should_run, msg = fn(
        news_ingest_enabled=True,
        rerun_enabled=False,
        boot_scout_enabled=True,
    )
    assert should_run is False
    assert "engine paused" in msg
    assert "rerun=false" in msg


def test_log_lines_distinguish_three_states():
    """Each of the three observable boot states (paused / scout-off /
    scout-on) must produce a distinct INFO log line so ops can tell at
    a glance from runtime logs which mode the boot is in."""
    fn = _helper()
    _, paused_msg = fn(
        news_ingest_enabled=False,
        rerun_enabled=True,
        boot_scout_enabled=False,
    )
    _, off_msg = fn(
        news_ingest_enabled=True,
        rerun_enabled=True,
        boot_scout_enabled=False,
    )
    _, on_msg = fn(
        news_ingest_enabled=True,
        rerun_enabled=True,
        boot_scout_enabled=True,
    )
    # Three distinct lines.
    assert paused_msg != off_msg
    assert off_msg != on_msg
    assert paused_msg != on_msg
    # Distinguishing tokens.
    assert "engine paused" in paused_msg
    assert "boot scout disabled" in off_msg
    assert "boot scout active" in on_msg


def test_log_message_emits_at_info(caplog):
    """Sanity: when the caller logs the helper's message at INFO, all
    three states surface at level INFO (not WARNING / ERROR)."""
    fn = _helper()
    logger = logging.getLogger("pulse-test-boot-scout")
    states = [
        (False, True, False),  # paused
        (True, True, False),   # scout off (default)
        (True, True, True),    # scout on
    ]
    with caplog.at_level(logging.INFO, logger=logger.name):
        for ni, rr, bs in states:
            _, msg = fn(
                news_ingest_enabled=ni,
                rerun_enabled=rr,
                boot_scout_enabled=bs,
            )
            logger.info(msg)
    levels = {r.levelname for r in caplog.records if r.name == logger.name}
    assert levels == {"INFO"}
    assert len(
        [r for r in caplog.records if r.name == logger.name]
    ) == 3


# ── Config-parser tests (env-var safety) ───────────────────────────────

def _reload_config_with_env(monkeypatch, value):
    """Reload app.config with PULSE_BOOT_SCOUT_ENABLED set (or unset if
    value is None) and return the freshly parsed module-level constant."""
    if value is None:
        monkeypatch.delenv("PULSE_BOOT_SCOUT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PULSE_BOOT_SCOUT_ENABLED", value)
    from app import config as _cfg
    importlib.reload(_cfg)
    return _cfg.PULSE_BOOT_SCOUT_ENABLED


def test_config_unset_defaults_to_false(monkeypatch):
    """Default-false is the whole point — zero LLM cost on redeploy
    unless an operator explicitly opts in."""
    assert _reload_config_with_env(monkeypatch, None) is False


def test_config_true_value_parses(monkeypatch):
    assert _reload_config_with_env(monkeypatch, "true") is True


def test_config_uppercase_true_parses(monkeypatch):
    """Lower-cased compare — TRUE / True / true all flip the switch."""
    assert _reload_config_with_env(monkeypatch, "TRUE") is True


def test_config_false_value_parses(monkeypatch):
    assert _reload_config_with_env(monkeypatch, "false") is False


def test_config_garbage_value_falls_back_to_false(monkeypatch):
    """Match the defensive convention: only an exact lower-case "true"
    flips the switch on. Anything else is OFF — safe default."""
    assert _reload_config_with_env(monkeypatch, "yes") is False
    assert _reload_config_with_env(monkeypatch, "1") is False


@pytest.fixture(autouse=True)
def _restore_config_module():
    """Ensure other tests in the suite see the unmutated module after
    we've reloaded it with monkeypatched env."""
    yield
    from app import config as _cfg
    importlib.reload(_cfg)
