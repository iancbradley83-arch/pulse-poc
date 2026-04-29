"""
Tests for help_topics.py and the /help changes in formatting.py.

Covers:
  - Each of the 10 topics returns non-empty text containing the command name
  - Slash prefix, mixed case, and bare name all return identical output
  - Unknown topic returns the "no help for" message
  - Bare /help regression: format_help() still includes existing sections
    and adds the new footer line
  - /help_status regex matches correctly
"""
import re

import pytest

import ops_bot.help_topics as ht
from ops_bot.formatting import format_help


# ---------------------------------------------------------------------------
# All 10 topics return non-empty text containing the command name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "status",
    "cost",
    "breakdown",
    "feed",
    "cards",
    "card",
    "embed",
    "logs",
    "runbook",
    "playbook",
    "env",
])
def test_each_topic_returns_nonempty_text_containing_command_name(command):
    result = ht.render(command)
    assert result, f"render('{command}') returned empty string"
    assert command in result.lower(), (
        f"render('{command}') doesn't contain the command name in output:\n{result}"
    )


# ---------------------------------------------------------------------------
# Normalisation: slash, case, leading/trailing whitespace
# ---------------------------------------------------------------------------

def test_slash_prefix_same_as_bare():
    assert ht.render("embed") == ht.render("/embed")


def test_leading_slash_same_as_bare_for_all_topics():
    for cmd in ht.TOPICS:
        assert ht.render(cmd) == ht.render(f"/{cmd}"), (
            f"render('{cmd}') != render('/{cmd}')"
        )


def test_uppercase_same_as_lowercase():
    assert ht.render("EMBED") == ht.render("embed")


def test_slash_and_uppercase_together():
    assert ht.render("/EMBED") == ht.render("embed")


# ---------------------------------------------------------------------------
# Unknown topic
# ---------------------------------------------------------------------------

def test_unknown_topic_returns_no_help_message():
    result = ht.render("doesnotexist")
    assert "no help for" in result


def test_unknown_topic_includes_topic_name_in_message():
    result = ht.render("foobar")
    assert "foobar" in result


def test_unknown_topic_suggests_help():
    result = ht.render("xyz")
    assert "/help" in result


# ---------------------------------------------------------------------------
# format_help regression — bare /help output unchanged + footer added
# ---------------------------------------------------------------------------

def test_format_help_includes_stage1_commands():
    out = format_help()
    assert "/status" in out
    assert "/cost" in out


def test_format_help_includes_stage2_commands():
    out = format_help()
    assert "/feed" in out
    assert "/card" in out
    assert "/embed" in out
    assert "/logs" in out
    assert "/runbook" in out
    assert "/env" in out


def test_format_help_lists_stage3_commands():
    """Stage 3 is now live (PR #97). /help should list each act-command, not 'coming'."""
    out = format_help()
    assert "stage 3" in out
    for cmd in ("/pause", "/resume", "/rerun", "/flag", "/redeploy", "/snooze"):
        assert cmd in out


def test_format_help_includes_new_footer():
    out = format_help()
    assert "/help <command>" in out
    # The tappable example must be present
    assert "/help_status" in out


def test_format_help_footer_is_last_meaningful_line():
    out = format_help()
    lines = out.rstrip().splitlines()
    assert "/help_status" in lines[-1], (
        f"Expected footer with /help_status on last line, got: {lines[-1]!r}"
    )


# ---------------------------------------------------------------------------
# /help_<command> regex
# ---------------------------------------------------------------------------

_HELP_UNDERSCORE_RE = re.compile(r"^/help_([a-z]+)(?:@\w+)?(?:\s|$)")


@pytest.mark.parametrize("text,expected_group", [
    ("/help_status", "status"),
    ("/help_cost", "cost"),
    ("/help_embed", "embed"),
    ("/help_status@mybot", "status"),
    ("/help_env ", "env"),
])
def test_help_underscore_regex_matches(text, expected_group):
    m = _HELP_UNDERSCORE_RE.match(text)
    assert m is not None, f"regex did not match: {text!r}"
    assert m.group(1) == expected_group


@pytest.mark.parametrize("text", [
    "/help status",   # space form — not matched by underscore regex
    "/helpstatus",    # no underscore
    "/HELP_status",   # uppercase (regex is lowercase only by design — handler lowercases)
    "/help_",         # empty topic
])
def test_help_underscore_regex_does_not_match_invalid(text):
    m = _HELP_UNDERSCORE_RE.match(text)
    assert m is None, f"regex should not match: {text!r}"
