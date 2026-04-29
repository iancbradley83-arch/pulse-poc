"""
Tests for snooze.py

Covers:
  - snooze sets, is_snoozed returns True within window
  - is_snoozed returns False after expiry
  - clear removes snooze
  - current() returns active snoozes only
  - parse_duration parses correctly
  - invalid kind raises ValueError
"""
import time
from unittest.mock import patch

import pytest

import ops_bot.snooze as s


def _reset():
    """Clear in-memory snooze state between tests."""
    s._snoozed.clear()


# ---------------------------------------------------------------------------
# snooze / is_snoozed
# ---------------------------------------------------------------------------

def test_snooze_sets_and_is_snoozed_returns_true():
    _reset()
    with patch.object(s, "_persist"):
        s.snooze("cost", 3600)
    assert s.is_snoozed("cost") is True


def test_is_snoozed_false_when_not_set():
    _reset()
    assert s.is_snoozed("cost") is False


def test_is_snoozed_false_after_expiry():
    _reset()
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with patch.object(s, "_persist"):
            s.snooze("cost", 60)
    with patch("ops_bot.snooze.time") as mock_time, patch.object(s, "_delete_from_db"):
        mock_time.monotonic.return_value = 1000.0 + 61
        result = s.is_snoozed("cost")
    assert result is False


def test_is_snoozed_true_just_before_expiry():
    _reset()
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with patch.object(s, "_persist"):
            s.snooze("cost", 60)
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0 + 59
        result = s.is_snoozed("cost")
    assert result is True


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

def test_clear_removes_snooze():
    _reset()
    with patch.object(s, "_persist"), patch.object(s, "_delete_from_db"):
        s.snooze("cost", 3600)
        s.clear("cost")
    assert s.is_snoozed("cost") is False


def test_clear_noop_when_not_set():
    _reset()
    with patch.object(s, "_delete_from_db"):
        s.clear("cost")  # should not raise


# ---------------------------------------------------------------------------
# current
# ---------------------------------------------------------------------------

def test_current_returns_active_only():
    _reset()
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with patch.object(s, "_persist"):
            s.snooze("cost", 3600)
            s.snooze("health", 10)
    with patch("ops_bot.snooze.time") as mock_time, patch.object(s, "_delete_from_db"):
        mock_time.monotonic.return_value = 1000.0 + 11  # health expired
        result = s.current()
    assert "cost" in result
    assert "health" not in result


def test_current_empty_when_none_set():
    _reset()
    result = s.current()
    assert result == {}


def test_current_includes_remaining_seconds():
    _reset()
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 0.0
        with patch.object(s, "_persist"):
            s.snooze("cost", 3600)
    with patch("ops_bot.snooze.time") as mock_time:
        mock_time.monotonic.return_value = 600.0
        result = s.current()
    assert "cost" in result
    remaining = result["cost"]["remaining_seconds"]
    assert 2990 <= remaining <= 3000


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------

def test_parse_duration_minutes():
    assert s.parse_duration("30m") == 30 * 60


def test_parse_duration_hours():
    assert s.parse_duration("1h") == 3600
    assert s.parse_duration("2h") == 7200


def test_parse_duration_off():
    assert s.parse_duration("off") == 0


def test_parse_duration_fractional():
    assert s.parse_duration("1.5h") == 5400


def test_parse_duration_unknown():
    assert s.parse_duration("3d") is None
    assert s.parse_duration("abc") is None
    assert s.parse_duration("") is None


# ---------------------------------------------------------------------------
# invalid kind
# ---------------------------------------------------------------------------

def test_invalid_kind_raises():
    # "deploy", "health", "feed" are now valid; use a truly invalid kind.
    with pytest.raises(ValueError, match="unknown snooze kind"):
        s.snooze("sentry", 3600)


# ---------------------------------------------------------------------------
# kind isolation
# ---------------------------------------------------------------------------

def test_cost_and_health_independent():
    _reset()
    with patch.object(s, "_persist"):
        s.snooze("cost", 3600)
    assert s.is_snoozed("cost") is True
    assert s.is_snoozed("health") is False
