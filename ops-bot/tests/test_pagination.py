"""
Tests for /feed page <n> formatting via format_feed_page.
"""
import pytest

from ops_bot.formatting import format_feed_page


def _cards(n):
    return [
        {
            "id": f"abcdefgh{i:02d}",
            "hook_type": "tactical",
            "game": {"league": {"name": "Premier League"}},
            "narrative_hook": f"Narrative for card {i}",
            "total_odds": 2.10 + i * 0.01,
        }
        for i in range(n)
    ]


def test_page_1_of_n_shows_footer():
    cards = _cards(10)
    text = format_feed_page(cards[:5], page=1, total_pages=2, total_cards=10)
    assert "page 1 of 2" in text
    assert "10 cards" in text


def test_last_page_partial():
    cards = _cards(3)
    text = format_feed_page(cards, page=2, total_pages=2, total_cards=8)
    assert "page 2 of 2" in text
    assert "8 cards" in text


def test_out_of_range_page_message():
    text = format_feed_page([], page=99, total_pages=2, total_cards=10)
    assert "no such page" in text
    assert "2 page" in text


def test_card_block_contains_id_prefix():
    cards = _cards(1)
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # id is abcdefgh00 — first 8 chars = abcdefgh, wrapped in [ ]
    assert "[abcdefgh]" in text


def test_narrative_truncated_at_word_boundary():
    long_narrative = (
        "Saka starts in Madrid; Arsenal's width reopens against a "
        "back four that has conceded twice in the past three games "
        "across all competitions, with substitutions tactical."
    )
    cards = [
        {
            "id": "abc12345",
            "hook_type": "news",
            "game": {
                "home_team": "Arsenal",
                "away_team": "Real Madrid",
                "league": {"name": "Champions League"},
            },
            "narrative_hook": long_narrative,
            "total_odds": 1.95,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # Should contain ellipsis (…) marker on truncation
    assert "…" in text
    # The full long narrative should not appear verbatim
    assert long_narrative not in text


def test_block_includes_game_and_league_lines():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "tactical",
            "game": {
                "home_team": "PSG",
                "away_team": "Bayern Munich",
                "league": {"name": "Champions League"},
            },
            "narrative_hook": "Short narrative",
            "total_odds": 4.10,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "PSG vs Bayern Munich" in text
    assert "Champions League" in text


def test_no_price_renders_as_no_price():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "news",
            "game": {"home_team": "Arsenal", "away_team": "Real Madrid"},
            "narrative_hook": "test",
            "total_odds": None,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "no price" in text


def test_suspended_card_flagged():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "tactical",
            "game": {"home_team": "Arsenal", "away_team": "Real Madrid"},
            "narrative_hook": "test",
            "total_odds": 2.0,
            "suspended": True,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "[SUSPENDED]" in text
