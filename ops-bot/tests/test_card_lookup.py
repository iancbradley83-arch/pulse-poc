"""
Tests for /card <id> card lookup and format_card_detail rendering.
"""
import pytest

from ops_bot.formatting import format_card_detail


# ---------------------------------------------------------------------------
# format_card_detail rendering
# ---------------------------------------------------------------------------

def _full_card():
    return {
        "id": "abc12345def67890",
        "bet_type": "single",
        "hook_type": "tactical",
        "game": {
            "home_team": "PSG",
            "away_team": "Bayern",
            "league": {"name": "Champions League"},
            "kickoff_time": "2026-04-29 19:00 UTC",
        },
        "narrative_hook": "PSG's high press will unlock space behind Bayern.",
        "headline": "PSG to capitalise on Bayern's defensive frailties.",
        "legs": [
            {"selection": "PSG to win", "price": 2.10},
        ],
        "total_odds": 2.10,
        "relevance_score": 0.84,
        "suspended": False,
        "deep_link": "https://example.com/card/abc12345",
        "published_at": "2020-01-01T00:00:00+00:00",
    }


def test_full_card_renders_all_fields():
    card = _full_card()
    text = format_card_detail(card)
    assert "abc12345" in text
    assert "single" in text
    assert "PSG vs Bayern" in text
    assert "Champions League" in text
    assert "tactical" in text
    assert "PSG's high press" in text
    assert "PSG to capitalise" in text  # headline different from narrative
    assert "PSG to win @ 2.10" in text
    assert "Total odds: 2.10" in text
    assert "Relevance: 0.84" in text
    assert "Suspended: no" in text
    assert "https://example.com/card/abc12345" in text


def test_suspended_card_shows_yes():
    card = _full_card()
    card["suspended"] = True
    text = format_card_detail(card)
    assert "Suspended: yes" in text


def test_missing_headline_skips_headline_line():
    card = _full_card()
    card["headline"] = None
    text = format_card_detail(card)
    assert "Headline:" not in text


def test_headline_same_as_narrative_skips_headline():
    """If headline == narrative_hook, don't render duplicate Headline line."""
    card = _full_card()
    card["headline"] = card["narrative_hook"]
    text = format_card_detail(card)
    assert text.count("PSG's high press") == 1


def test_missing_game_skips_game_line():
    card = _full_card()
    card["game"] = {}
    text = format_card_detail(card)
    assert "vs" not in text


def test_missing_total_odds_skips_total_odds():
    card = _full_card()
    card["total_odds"] = None
    text = format_card_detail(card)
    assert "Total odds:" not in text


def test_no_legs_skips_legs_section():
    card = _full_card()
    card["legs"] = []
    text = format_card_detail(card)
    assert "Legs:" not in text


def test_leg_with_null_price_renders_without_at():
    card = _full_card()
    card["legs"] = [{"selection": "Win", "price": None}]
    text = format_card_detail(card)
    assert "Win" in text
    # Should not have " @ " for null price
    assert " @ " not in text


# ---------------------------------------------------------------------------
# Card lookup helpers (test the search logic inline)
# ---------------------------------------------------------------------------

def _find_card(cards, card_id):
    """Replicate the handler's search logic for testing."""
    matched = None
    for card in cards:
        cid = card.get("id") or ""
        if cid == card_id:
            matched = card
            break
    if matched is None and len(card_id) <= 8:
        for card in cards:
            cid = card.get("id") or ""
            if cid[:8] == card_id[:8]:
                matched = card
                break
    return matched


def test_full_id_match():
    cards = [{"id": "abc12345def67890"}, {"id": "xyz99999"}]
    assert _find_card(cards, "abc12345def67890")["id"] == "abc12345def67890"


def test_prefix_match():
    cards = [{"id": "abc12345def67890"}, {"id": "xyz99999"}]
    result = _find_card(cards, "abc12345")
    assert result is not None
    assert result["id"] == "abc12345def67890"


def test_no_match_returns_none():
    cards = [{"id": "abc12345def67890"}]
    assert _find_card(cards, "xxxxxxxx") is None


def test_full_id_takes_priority_over_prefix():
    """Exact match should be preferred over prefix match of another card."""
    cards = [
        {"id": "abc12345def67890"},
        {"id": "abc12345"},  # shorter, exact match on this one
    ]
    result = _find_card(cards, "abc12345")
    assert result["id"] == "abc12345"
