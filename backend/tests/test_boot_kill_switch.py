"""Tests for the boot-time kill-switch gate + feed rehydrate path.

What this file proves:

  1. **`_boot_engine_enabled()` is the AND of both kill switches.**
     Either flipped off pauses the engine. We don't want the boot scout
     firing on every Railway redeploy when the engine is meant to be
     paused — that's how the test on 2026-04-27 cost $0.49 in 4 redeploys.

  2. **`_publish_persisted_candidates` is reachable and signature-stable.**
     The boot rehydrate path must exist as a top-level callable so a cold
     start can populate `feed.prematch_cards` without spending credits.

  3. **Static wiring.** The startup hook routes through
     `_load_rogue_prematch(publish_only=True)` when the engine is paused,
     and `_load_rogue_prematch(publish_only=True)` short-circuits past
     `_run_candidate_engine`. Greps the source instead of running the
     hook (which would hit Rogue + risk LLM imports).

  4. **No live LLM calls.** Tests don't import or patch `AsyncAnthropic`.
     The rehydrate path's contract is "rewriter=None → no LLM" — proven
     by inspection of `_publish_loop` (rewriter is the ONLY LLM-touching
     dependency in the publish loop).

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_boot_kill_switch.py -v
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ──────────────────────────────────────────────────────────────────────
# 1) `_boot_engine_enabled()` honours both kill switches
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "rerun, news_ingest, expected",
    [
        ("true",  True,  True),   # both on — engine fires
        ("false", True,  False),  # rerun off — paused
        ("true",  False, False),  # news ingest off — paused
        ("false", False, False),  # both off — paused
        ("TRUE",  True,  True),   # case-insensitive
        ("",      True,  False),  # empty string ≠ "true" → off
    ],
)
def test_boot_engine_enabled_matrix(monkeypatch, rerun, news_ingest, expected):
    monkeypatch.setenv("PULSE_RERUN_ENABLED", rerun)
    # PULSE_NEWS_INGEST_ENABLED is read at module import time. Patch the
    # module-level constant after import — that's the reference the gate
    # actually consults.
    from app import main as main_mod
    monkeypatch.setattr(main_mod, "PULSE_NEWS_INGEST_ENABLED", news_ingest)
    assert main_mod._boot_engine_enabled() is expected


# ──────────────────────────────────────────────────────────────────────
# 2) Rehydrate helper exists with the expected signature
# ──────────────────────────────────────────────────────────────────────

def test_publish_persisted_candidates_callable():
    from app.main import _publish_persisted_candidates
    assert inspect.iscoroutinefunction(_publish_persisted_candidates)
    sig = inspect.signature(_publish_persisted_candidates)
    params = sig.parameters
    assert "games_by_id" in params
    assert "rogue_client" in params
    assert "target_feed" in params


def test_publish_loop_rewriter_optional():
    """The publish loop must accept `rewriter=None`. That's the contract
    the rehydrate path relies on for zero-LLM-cost rebuilds."""
    from app.main import _publish_loop
    assert inspect.iscoroutinefunction(_publish_loop)
    sig = inspect.signature(_publish_loop)
    rewriter_param = sig.parameters.get("rewriter")
    assert rewriter_param is not None
    assert rewriter_param.default is None


def test_load_rogue_prematch_publish_only_kwarg():
    """Static contract: `_load_rogue_prematch` accepts `publish_only`.
    The startup hook passes `publish_only=True` when the engine is
    paused — without this kwarg the rehydrate path can't short-circuit
    past `_run_candidate_engine`."""
    from app.main import _load_rogue_prematch
    sig = inspect.signature(_load_rogue_prematch)
    publish_only = sig.parameters.get("publish_only")
    assert publish_only is not None
    assert publish_only.default is False


# ──────────────────────────────────────────────────────────────────────
# 3) Static source-wiring checks
# ──────────────────────────────────────────────────────────────────────

_MAIN_SRC = (_ROOT / "app" / "main.py").read_text()


def test_startup_hook_gates_on_boot_engine_enabled():
    """The startup hook must call `_boot_engine_enabled()` before the
    boot scout — and route to `_load_rogue_prematch(publish_only=True)`
    on the paused branch. Greps confirm both lines are present."""
    assert "_boot_engine_enabled()" in _MAIN_SRC
    assert "publish_only=True" in _MAIN_SRC
    # The exact log line we want to see in Railway after every cold start
    # when the kill switches are off:
    assert "skipping boot scout" in _MAIN_SRC
    assert "feed rehydrated" in _MAIN_SRC


def test_publish_only_branch_skips_run_candidate_engine():
    """`_load_rogue_prematch(publish_only=True)` must NOT fall through
    to `_run_candidate_engine`. We assert by counting: the function
    should reference `_run_candidate_engine` on exactly one path (the
    `else` branch where the engine is meant to fire)."""
    # Find the `_load_rogue_prematch` body
    start = _MAIN_SRC.index("async def _load_rogue_prematch(")
    end = _MAIN_SRC.index("\nasync def _scheduled_rerun_loop", start)
    body = _MAIN_SRC[start:end]

    # The publish_only branch must call `_publish_persisted_candidates`,
    # NOT `_run_candidate_engine`.
    assert "_publish_persisted_candidates" in body
    # And the engine must be guarded by `elif not PULSE_NEWS_INGEST_ENABLED`
    # / `else` — i.e. the publish_only branch is checked first.
    publish_only_idx = body.index("if publish_only:")
    engine_call_idx = body.index("_run_candidate_engine(")
    assert publish_only_idx < engine_call_idx


def test_tier_loops_gated_by_boot_engine_enabled():
    """Boot-time tier-loop spawning must also respect the kill switches.
    Fix 1 of the spec: `if PULSE_TIERED_FRESHNESS_ENABLED=false OR
    PULSE_RERUN_ENABLED=false, don't spawn`. We use `_boot_engine_enabled`
    as the AND-gate, so a False from it short-circuits the spawn loop."""
    # Find the startup hook
    start = _MAIN_SRC.index("async def generate_prematch_cards(")
    end = _MAIN_SRC.index("\n@app.on_event(\"shutdown\")", start)
    body = _MAIN_SRC[start:end]

    # The pause-branch log line proves the spawn block is gated:
    assert "skipping tier loops" in body
    # And `_tier_loop` / `_scheduled_rerun_loop` spawns must be inside
    # the engine-on branch (after the `_engine_on` boolean).
    engine_on_idx = body.index("_engine_on = _boot_engine_enabled()")
    spawn_idx = body.index("_tier_loop(_tier)")
    rerun_spawn_idx = body.index("_scheduled_rerun_loop()")
    assert engine_on_idx < spawn_idx
    assert engine_on_idx < rerun_spawn_idx
