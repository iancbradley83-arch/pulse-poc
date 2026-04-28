"""
Tests for /env <key> — format_env_var secret scrubbing.
"""
import pytest

from ops_bot.formatting import format_env_var


# ---------------------------------------------------------------------------
# Secret scrubbing — keys matching (?i)(token|secret|key|pass|jwt|api)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "TELEGRAM_BOT_TOKEN",
    "token",
    "SECRET_KEY",
    "MY_SECRET",
    "DB_PASSWORD",
    "JWT_SECRET",
    "ANTHROPIC_API_KEY",
    "RAILWAY_API_TOKEN",
    "PULSE_ADMIN_PASS",
    "api_key",
    "API_TOKEN",
])
def test_secret_key_is_scrubbed(key):
    value = "supersecretvalue1234"
    text = format_env_var(key, value)
    assert "***" in text
    # First 8 chars of value shown
    assert value[:8] in text
    # Full value not shown
    assert value not in text
    assert "<scrubbed>" in text


@pytest.mark.parametrize("key", [
    "PULSE_RERUN_ENABLED",
    "RAILWAY_ENVIRONMENT",
    "PULSE_DATA_SOURCE",
    "PULSE_LOG_LEVEL",
    "PORT",
    "HOSTNAME",
])
def test_plain_key_not_scrubbed(key):
    value = "some-plain-value"
    text = format_env_var(key, value)
    assert value in text
    assert "***" not in text
    assert "<scrubbed>" not in text


def test_missing_key_shows_not_set():
    text = format_env_var("SOME_VAR", None)
    assert "SOME_VAR is not set" in text


def test_railway_unreachable():
    text = format_env_var("ANY_KEY", None, railway_unreachable=True)
    assert "Railway API unreachable" in text


def test_short_secret_value_still_scrubbed():
    """Value shorter than 8 chars still gets partial scrub."""
    text = format_env_var("MY_TOKEN", "abc")
    assert "***" in text
    assert "<scrubbed>" in text


def test_plain_key_shows_full_value():
    text = format_env_var("PULSE_LOG_LEVEL", "DEBUG")
    assert "DEBUG" in text
    assert "PULSE_LOG_LEVEL" in text
