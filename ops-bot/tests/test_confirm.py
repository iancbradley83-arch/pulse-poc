"""
Tests for confirm.py

Covers:
  - register / resolve happy path
  - expired confirm returns None
  - second action for same chat overrides first (resolve on new action_id mismatch)
  - resolve wrong action_id returns None (but preserves pending)
  - peek reads without consuming
  - expire_old prunes expired entries
  - pending_action_id helper
"""
import time
from unittest.mock import patch

import pytest

import ops_bot.confirm as c


def _reset():
    """Clear module-level state between tests."""
    c._pending.clear()


# ---------------------------------------------------------------------------
# register / resolve
# ---------------------------------------------------------------------------

def test_register_and_resolve_happy_path():
    """args=None must NOT look like 'not found'. resolve returns (found, args)."""
    _reset()
    c.register(100, "pause", None)
    found, args = c.resolve(100, "pause")
    assert found is True
    assert args is None


def test_register_and_resolve_returns_args():
    _reset()
    c.register(100, "flag", ("MY_VAR", "true"))
    found, args = c.resolve(100, "flag")
    assert found is True
    assert args == ("MY_VAR", "true")


def test_resolve_removes_entry():
    _reset()
    c.register(100, "rerun", None)
    c.resolve(100, "rerun")
    # Second resolve should return (False, None) (entry gone).
    found, args = c.resolve(100, "rerun")
    assert found is False
    assert args is None


def test_resolve_no_entry_returns_not_found():
    _reset()
    found, args = c.resolve(999, "pause")
    assert found is False
    assert args is None


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------

def test_expired_confirm_returns_not_found():
    _reset()
    # Register with a past expiry.
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        c.register(100, "pause", None)
    # Now time is past the TTL.
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0 + c.CONFIRM_TTL + 1
        found, _ = c.resolve(100, "pause")
    assert found is False


# ---------------------------------------------------------------------------
# second action overrides first
# ---------------------------------------------------------------------------

def test_second_register_overrides_first():
    _reset()
    c.register(100, "pause", None)
    c.register(100, "resume", None)
    # Second register overwrites — stored action_id is now "resume".
    # resolve("pause") sees stored="resume" != "pause" -> puts it back, returns (False, None).
    found_p, _ = c.resolve(100, "pause")
    assert found_p is False
    # The entry for "resume" is still there.
    found_r, args_r = c.resolve(100, "resume")
    assert found_r is True
    assert args_r is None


def test_second_register_replaces_ttl():
    """Registering again resets the expiry clock."""
    _reset()
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 0.0
        c.register(100, "pause", None)
        # Simulate time advancing past first TTL but still within second.
        mock_time.monotonic.return_value = c.CONFIRM_TTL - 1
        c.register(100, "pause", "args2")
        mock_time.monotonic.return_value = c.CONFIRM_TTL + 2
        # Original would have expired but new registration is still live.
        found, args = c.resolve(100, "pause")
    assert found is True
    assert args == "args2"


# ---------------------------------------------------------------------------
# peek
# ---------------------------------------------------------------------------

def test_peek_does_not_consume():
    _reset()
    c.register(100, "redeploy", "myargs")
    entry1 = c.peek(100)
    entry2 = c.peek(100)
    assert entry1 is not None
    assert entry2 is not None
    # Should still resolve.
    found, args = c.resolve(100, "redeploy")
    assert found is True
    assert args == "myargs"


def test_peek_expired_returns_none():
    _reset()
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 500.0
        c.register(100, "pause", None)
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 500.0 + c.CONFIRM_TTL + 5
        result = c.peek(100)
    assert result is None


# ---------------------------------------------------------------------------
# expire_old
# ---------------------------------------------------------------------------

def test_expire_old_removes_expired():
    _reset()
    with patch("ops_bot.confirm.time") as mock_time:
        mock_time.monotonic.return_value = 100.0
        c.register(1, "pause", None)
        c.register(2, "resume", None)
    with patch("ops_bot.confirm.time") as mock_time:
        # chat 1 expired, chat 2 still live.
        mock_time.monotonic.return_value = 100.0 + c.CONFIRM_TTL - 5
        c._pending[1] = ("pause", 100.0 + c.CONFIRM_TTL - 10, None)  # force expired
        removed = c.expire_old()
    assert removed >= 1


# ---------------------------------------------------------------------------
# pending_action_id
# ---------------------------------------------------------------------------

def test_pending_action_id_returns_action():
    _reset()
    c.register(100, "flag", ("X", "y"))
    action_id = c.pending_action_id(100)
    assert action_id == "flag"


def test_pending_action_id_none_when_not_pending():
    _reset()
    assert c.pending_action_id(999) is None


# ---------------------------------------------------------------------------
# multi-chat isolation
# ---------------------------------------------------------------------------

def test_different_chats_do_not_interfere():
    _reset()
    c.register(1, "pause", "a1")
    c.register(2, "rerun", "a2")
    found1, args1 = c.resolve(1, "pause")
    found2, args2 = c.resolve(2, "rerun")
    assert (found1, args1) == (True, "a1")
    assert (found2, args2) == (True, "a2")
