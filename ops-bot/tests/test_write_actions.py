"""
Tests for write_actions.py

Each action calls the right Railway/Pulse method with the right args (mocked).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ops_bot import write_actions as wa
from ops_bot.railway_client import RailwayError
from ops_bot.pulse_client import PulseError
from ops_bot.config import RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_sets_both_vars_to_false():
    rc = MagicMock()
    rc.set_variable = AsyncMock(return_value=None)

    success, summary = await wa.pause(rc)

    assert success is True
    assert "PULSE_RERUN_ENABLED" in summary
    assert "PULSE_NEWS_INGEST_ENABLED" in summary

    calls = rc.set_variable.call_args_list
    call_args = [(c.args[3], c.args[4]) for c in calls]
    assert ("PULSE_RERUN_ENABLED", "false") in call_args
    assert ("PULSE_NEWS_INGEST_ENABLED", "false") in call_args


@pytest.mark.asyncio
async def test_pause_passes_correct_project_service_env():
    rc = MagicMock()
    rc.set_variable = AsyncMock(return_value=None)

    await wa.pause(rc)

    for call in rc.set_variable.call_args_list:
        assert call.args[0] == RAILWAY_PROJECT_ID
        assert call.args[1] == RAILWAY_ENVIRONMENT_ID
        assert call.args[2] == RAILWAY_SERVICE_ID


@pytest.mark.asyncio
async def test_pause_partial_failure_returns_false(monkeypatch):
    """First var succeeds. Second var fails on initial call AND on retry —
    only then does it count as a real failure to the user."""
    monkeypatch.setattr("ops_bot.write_actions.asyncio.sleep", AsyncMock())
    rc = MagicMock()
    # var 1: ok. var 2: initial call fails, retry also fails.
    rc.set_variable = AsyncMock(
        side_effect=[None, RailwayError("boom"), RailwayError("boom-retry")]
    )

    success, summary = await wa.pause(rc)

    assert success is False
    assert "partial" in summary
    # Verify retry was attempted (3 calls total: ok + fail + fail-retry)
    assert rc.set_variable.await_count == 3


@pytest.mark.asyncio
async def test_pause_recovers_on_transient_railway_error(monkeypatch):
    """Single transient failure followed by success should report success
    (this is the 2026-05-01 /resume scenario — Railway hiccup, retry works)."""
    monkeypatch.setattr("ops_bot.write_actions.asyncio.sleep", AsyncMock())
    rc = MagicMock()
    # var 1: transient fail then retry-success. var 2: clean success.
    rc.set_variable = AsyncMock(
        side_effect=[RailwayError("transient"), None, None]
    )

    success, summary = await wa.pause(rc)

    assert success is True
    assert rc.set_variable.await_count == 3  # 1 fail + 1 retry-ok + 1 ok


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_sets_both_vars_to_true():
    rc = MagicMock()
    rc.set_variable = AsyncMock(return_value=None)

    success, summary = await wa.resume(rc)

    assert success is True
    calls = rc.set_variable.call_args_list
    call_args = [(c.args[3], c.args[4]) for c in calls]
    assert ("PULSE_RERUN_ENABLED", "true") in call_args
    assert ("PULSE_NEWS_INGEST_ENABLED", "true") in call_args


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerun_calls_post_admin_rerun():
    pc = MagicMock()
    pc.post_admin_rerun = AsyncMock(return_value={"ok": True})

    success, summary = await wa.rerun(pc)

    pc.post_admin_rerun.assert_awaited_once()
    assert success is True


@pytest.mark.asyncio
async def test_rerun_pulse_error_returns_false():
    pc = MagicMock()
    pc.post_admin_rerun = AsyncMock(side_effect=PulseError("unreachable"))

    success, summary = await wa.rerun(pc)

    assert success is False
    assert "unreachable" in summary.lower()


@pytest.mark.asyncio
async def test_rerun_ok_false_returns_false():
    pc = MagicMock()
    pc.post_admin_rerun = AsyncMock(return_value={"ok": False, "error": "busy"})

    success, summary = await wa.rerun(pc)

    assert success is False


# ---------------------------------------------------------------------------
# flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_calls_set_variable_with_correct_args():
    rc = MagicMock()
    rc.set_variable = AsyncMock(return_value=None)

    success, summary = await wa.flag(rc, "MY_FLAG", "true")

    rc.set_variable.assert_awaited_once_with(
        RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
        "MY_FLAG", "true",
    )
    assert success is True
    assert "MY_FLAG=true" in summary


@pytest.mark.asyncio
async def test_flag_railway_error_returns_false():
    rc = MagicMock()
    rc.set_variable = AsyncMock(side_effect=RailwayError("network"))

    success, summary = await wa.flag(rc, "X", "false")

    assert success is False


# ---------------------------------------------------------------------------
# redeploy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redeploy_looks_up_latest_then_calls_mutation():
    rc = MagicMock()
    rc.latest_deployment = AsyncMock(return_value={"id": "dep-abc", "status": "SUCCESS"})
    rc.redeploy_latest = AsyncMock(return_value={"id": "dep-xyz", "status": "INITIALIZING"})

    success, summary = await wa.redeploy(rc)

    rc.latest_deployment.assert_awaited_once_with(RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID)
    rc.redeploy_latest.assert_awaited_once_with("dep-abc")
    assert success is True
    assert "dep-xyz" in summary


@pytest.mark.asyncio
async def test_redeploy_latest_deployment_error_returns_false():
    rc = MagicMock()
    rc.latest_deployment = AsyncMock(side_effect=RailwayError("no deployments"))

    success, summary = await wa.redeploy(rc)

    assert success is False
    rc.redeploy_latest.assert_not_called()


@pytest.mark.asyncio
async def test_redeploy_mutation_error_returns_false():
    rc = MagicMock()
    rc.latest_deployment = AsyncMock(return_value={"id": "dep-abc", "status": "SUCCESS"})
    rc.redeploy_latest = AsyncMock(side_effect=RailwayError("mutation failed"))

    success, summary = await wa.redeploy(rc)

    assert success is False
