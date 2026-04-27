"""Tests for `estimate_cost_from_usage` — Haiku 4.5 per-MTOKEN pricing.

What this file proves:

  1. The module-level helper computes USD cost from real Anthropic
     `response.usage` token counts using Haiku 4.5 published rates.
  2. Cache-read tokens are billed at 10% of input tokens.
  3. Cache-write tokens are billed at the 5m default rate (or 1h when
     opted in).
  4. `web_search_count` adds the per-call websearch fee on top.
  5. The legacy per-call coefficients (PULSE_COST_HAIKU_PER_CALL,
     PULSE_COST_SONNET_*) no longer exist on `app.config`.

No live LLM calls — usage blocks are constructed by hand.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_cost_haiku_pricing.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_no_legacy_per_call_coefficients_on_config():
    """The legacy per-call coefficients must be gone from app.config."""
    from app import config

    for name in (
        "PULSE_COST_HAIKU_PER_CALL",
        "PULSE_COST_HAIKU_WEBSEARCH_PER_CALL",
        "PULSE_COST_SONNET_PER_CALL",
    ):
        assert not hasattr(config, name), (
            f"app.config still exposes legacy coefficient {name} — should "
            "have been deleted in fix/cost-coeffs-and-hot-classifier."
        )


def test_estimate_input_output_tokens():
    """1000 input + 200 output tokens at Haiku rates → $0.001 + $0.001 = $0.002."""
    from app.services.cost_tracker import estimate_cost_from_usage

    usage = {"input_tokens": 1000, "output_tokens": 200}
    cost = estimate_cost_from_usage(usage)
    # 1000 * 1 / 1e6 = 0.001 ; 200 * 5 / 1e6 = 0.001 ; total 0.002
    assert cost == pytest.approx(0.002, rel=0.01)


def test_estimate_with_cache_read_at_ten_percent_of_input():
    """Cache-read tokens billed at 10% of input rate ($0.10 / MTOK)."""
    from app.services.cost_tracker import estimate_cost_from_usage

    # 100 input, 0 output, 1000 cache_read.
    # 100 * 1 / 1e6 = 0.0001
    # 1000 * 0.10 / 1e6 = 0.0001
    usage = {
        "input_tokens": 100,
        "output_tokens": 0,
        "cache_read_input_tokens": 1000,
    }
    cost = estimate_cost_from_usage(usage)
    assert cost == pytest.approx(0.0002, rel=0.01)


def test_estimate_with_cache_write_5m():
    """Cache-write tokens billed at $1.25/MTOK (5m default)."""
    from app.services.cost_tracker import estimate_cost_from_usage

    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1000,
    }
    cost = estimate_cost_from_usage(usage)
    # 1000 * 1.25 / 1e6 = 0.00125
    assert cost == pytest.approx(0.00125, rel=0.01)


def test_estimate_with_cache_write_1h():
    """Opt-in `cache_write_ttl="1h"` switches to $2/MTOK."""
    from app.services.cost_tracker import estimate_cost_from_usage

    usage = {"cache_creation_input_tokens": 1000}
    cost = estimate_cost_from_usage(usage, cache_write_ttl="1h")
    # 1000 * 2.0 / 1e6 = 0.002
    assert cost == pytest.approx(0.002, rel=0.01)


def test_websearch_count_adds_per_call_fee():
    """Each web_search invocation adds $0.025."""
    from app.services.cost_tracker import estimate_cost_from_usage

    # No tokens at all — pure websearch fee.
    usage = {"input_tokens": 0, "output_tokens": 0}
    cost = estimate_cost_from_usage(usage, web_search_count=4)
    # 4 * 0.025 = 0.10
    assert cost == pytest.approx(0.10, rel=0.001)


def test_estimate_handles_none_usage():
    """`None` usage returns 0.0 (partial-response safety)."""
    from app.services.cost_tracker import estimate_cost_from_usage

    assert estimate_cost_from_usage(None) == 0.0


def test_estimate_handles_object_usage():
    """SDK Usage objects (attribute access) work the same as dicts."""
    from app.services.cost_tracker import estimate_cost_from_usage

    class FakeUsage:
        input_tokens = 1000
        output_tokens = 200
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    cost = estimate_cost_from_usage(FakeUsage())
    assert cost == pytest.approx(0.002, rel=0.01)


def test_full_blended_call_within_one_percent_of_anthropic_math():
    """End-to-end: blended call (input + output + cache_read + websearch)."""
    from app.services.cost_tracker import estimate_cost_from_usage

    usage = {
        "input_tokens": 2000,         # 2000 * 1 / 1e6 = 0.002
        "output_tokens": 500,         # 500 * 5 / 1e6 = 0.0025
        "cache_read_input_tokens": 5000,   # 5000 * 0.10 / 1e6 = 0.0005
        "cache_creation_input_tokens": 1500,  # 1500 * 1.25 / 1e6 = 0.001875
    }
    cost = estimate_cost_from_usage(usage, web_search_count=2)
    # tokens cost: 0.002 + 0.0025 + 0.0005 + 0.001875 = 0.006875
    # web_search:  2 * 0.025 = 0.05
    # total:       0.056875
    assert cost == pytest.approx(0.056875, rel=0.01)
