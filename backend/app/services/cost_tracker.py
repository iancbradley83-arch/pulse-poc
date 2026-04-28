"""CostTracker — daily LLM-spend tripwire.

Cost-aware redesign (2026-04-26). Tracks accumulated USD spend per UTC
day and short-circuits LLM calls when the configured budget is
exhausted. Persists to `daily_cost` table on `CandidateStore` so the
counter survives Railway redeploys (the volume is the source of truth;
without persistence a redeploy at 23:30 UTC would reset the counter and
let the engine spend a full second budget overnight).

Design rules:

  * **One source of truth.** `today_total_usd()` reads from SQLite,
    not from a process-local cache. Two engine processes (cold-start
    rerun + scheduled tier loop) cannot blow past budget by reading
    stale per-process state.
  * **Pre-call estimate, post-call actual.** `can_spend(projected)`
    rejects calls whose projected cost would push us over budget *to
    a 99% threshold*; the 1% slack absorbs single-token-counter drift
    so we don't soft-block an otherwise-cheap call. After the call
    completes, `record_call(actual)` writes the true cost based on
    the response.usage tokens.
  * **Graceful degradation.** Every caller treats `can_spend(...) =
    False` as "skip this call and return the cached/empty result";
    we never throw on budget exhaustion.

Pricing coefficients are env-knobbed (`PULSE_COST_*`) so we can adjust
without a redeploy. Defaults track Anthropic's published Haiku 4.5
prices; web_search is the standard add-on. See
`backend/app/config.py` for the full knob list.
"""
from __future__ import annotations

import contextvars
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Optional contextual override for the `kind` recorded against a call.
# The boot-scout path (`_load_rogue_prematch` on startup) sets this to
# `"boot_scout"` so today's redeploy churn is attributable cleanly,
# without forcing every NewsIngester call site to grow a new arg. When
# unset, `record_call` uses the `kind` it was passed.
_kind_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "pulse_cost_kind_override", default=None,
)


def set_kind_override(kind: Optional[str]) -> Any:
    """Set the per-context `kind` override used by record_call.

    Returns the contextvar Token so callers can `reset()` when their
    scope ends. Pass `None` to clear.
    """
    return _kind_override.set(kind)


def reset_kind_override(token: Any) -> None:
    """Reset the kind-override contextvar to the value before `set_kind_override`."""
    try:
        _kind_override.reset(token)
    except (ValueError, LookupError):
        # Token from a different context; safe to ignore — the new
        # context's default (None) will apply.
        pass


# ── Budget + pricing knobs ─────────────────────────────────────────────
# Read at import time. Tests can monkey-patch via env or by passing
# explicit overrides on the constructor.

def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Daily budget. $3 conservative for the POC — was set to $10 originally
# but the 6-day spend leak ran to $407 before a tripwire fired, so the
# 2026-04-26 redesign halved the ceiling.
DEFAULT_DAILY_LLM_BUDGET_USD = _envf("PULSE_DAILY_LLM_BUDGET_USD", 3.0)
DEFAULT_DAILY_WEBSEARCH_BUDGET = _envi("PULSE_DAILY_WEBSEARCH_BUDGET", 100)

# Per-million-token rates (Anthropic public Haiku 4.5 pricing).
DEFAULT_HAIKU_INPUT_PER_MTOKEN_USD = _envf(
    "PULSE_COST_HAIKU_INPUT_PER_MTOKEN_USD", 1.0,
)
DEFAULT_HAIKU_OUTPUT_PER_MTOKEN_USD = _envf(
    "PULSE_COST_HAIKU_OUTPUT_PER_MTOKEN_USD", 5.0,
)
DEFAULT_HAIKU_CACHE_READ_PER_MTOKEN_USD = _envf(
    "PULSE_COST_HAIKU_CACHE_READ_PER_MTOKEN_USD", 0.10,
)
DEFAULT_HAIKU_CACHE_WRITE_5M_PER_MTOKEN_USD = _envf(
    "PULSE_COST_HAIKU_CACHE_WRITE_PER_MTOKEN_USD", 1.25,
)
DEFAULT_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD = _envf(
    "PULSE_COST_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD", 2.0,
)
DEFAULT_WEBSEARCH_PER_CALL_USD = _envf(
    "PULSE_COST_WEBSEARCH_PER_CALL_USD", 0.025,
)


def today_utc() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def estimate_cost_from_usage(
    response_usage: Any,
    *,
    web_search_count: int = 0,
    cache_write_ttl: str = "5m",
) -> float:
    """Module-level helper — compute USD cost from an Anthropic usage block.

    Mirrors `CostTracker.cost_from_usage` but uses the module-level
    Haiku 4.5 default rates so callers without a tracker instance can
    still produce an honest cost number from `response.usage` token
    counts (input/output/cache_read/cache_creation) plus a
    `web_search_count` add-on. The default-rates path is what every
    Haiku call site uses post-PR #62.

    `response_usage` may be the SDK's Usage object or a dict with the
    same fields. Missing fields default to 0 — partial responses still
    produce a sensible (lower-bound) cost.
    """
    if response_usage is None:
        return 0.0

    def _g(name: str) -> int:
        if isinstance(response_usage, dict):
            v = response_usage.get(name)
        else:
            v = getattr(response_usage, name, None)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _g("input_tokens")
    output_tokens = _g("output_tokens")
    cache_read = _g("cache_read_input_tokens")
    cache_write = _g("cache_creation_input_tokens")

    cost = (
        input_tokens * DEFAULT_HAIKU_INPUT_PER_MTOKEN_USD / 1_000_000.0
        + output_tokens * DEFAULT_HAIKU_OUTPUT_PER_MTOKEN_USD / 1_000_000.0
        + cache_read * DEFAULT_HAIKU_CACHE_READ_PER_MTOKEN_USD / 1_000_000.0
    )
    if cache_write:
        rate = (
            DEFAULT_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD
            if str(cache_write_ttl).lower() == "1h"
            else DEFAULT_HAIKU_CACHE_WRITE_5M_PER_MTOKEN_USD
        )
        cost += cache_write * rate / 1_000_000.0
    n_search = max(0, int(web_search_count or 0))
    if n_search > 0:
        cost += n_search * DEFAULT_WEBSEARCH_PER_CALL_USD
    return cost


class CostTracker:
    """Per-process daily-cost tracker backed by the candidate_store."""

    def __init__(
        self,
        store: "Any",
        *,
        daily_budget_usd: Optional[float] = None,
        websearch_daily_budget: Optional[int] = None,
        haiku_input_per_mtoken: Optional[float] = None,
        haiku_output_per_mtoken: Optional[float] = None,
        haiku_cache_read_per_mtoken: Optional[float] = None,
        haiku_cache_write_5m_per_mtoken: Optional[float] = None,
        haiku_cache_write_1h_per_mtoken: Optional[float] = None,
        websearch_per_call_usd: Optional[float] = None,
    ):
        self._store = store
        self._daily_budget = (
            float(daily_budget_usd)
            if daily_budget_usd is not None
            else DEFAULT_DAILY_LLM_BUDGET_USD
        )
        self._websearch_daily_budget = (
            int(websearch_daily_budget)
            if websearch_daily_budget is not None
            else DEFAULT_DAILY_WEBSEARCH_BUDGET
        )
        self._r_input = (
            haiku_input_per_mtoken
            if haiku_input_per_mtoken is not None
            else DEFAULT_HAIKU_INPUT_PER_MTOKEN_USD
        )
        self._r_output = (
            haiku_output_per_mtoken
            if haiku_output_per_mtoken is not None
            else DEFAULT_HAIKU_OUTPUT_PER_MTOKEN_USD
        )
        self._r_cache_read = (
            haiku_cache_read_per_mtoken
            if haiku_cache_read_per_mtoken is not None
            else DEFAULT_HAIKU_CACHE_READ_PER_MTOKEN_USD
        )
        self._r_cache_write_5m = (
            haiku_cache_write_5m_per_mtoken
            if haiku_cache_write_5m_per_mtoken is not None
            else DEFAULT_HAIKU_CACHE_WRITE_5M_PER_MTOKEN_USD
        )
        self._r_cache_write_1h = (
            haiku_cache_write_1h_per_mtoken
            if haiku_cache_write_1h_per_mtoken is not None
            else DEFAULT_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD
        )
        self._r_websearch = (
            websearch_per_call_usd
            if websearch_per_call_usd is not None
            else DEFAULT_WEBSEARCH_PER_CALL_USD
        )
        # 80% budget alert state removed 2026-04-28 — pulse-ops-bot now
        # owns the alert ladder ($1/$2/$2.95). Tripwire alert (the
        # critical, dedup'd one) still lives in `can_spend` via
        # `alert_emitter.emit_critical`.

    # ── Cost arithmetic helpers ────────────────────────────────────────

    def estimate_haiku_call(
        self,
        *,
        input_tokens: int,
        max_output_tokens: int,
        web_search: bool = False,
        web_search_calls: int = 0,
    ) -> float:
        """Return a conservative pre-call USD estimate.

        Assumes worst-case cache miss on the system prompt — pre-call we
        don't know if Anthropic's cache will hit. Output is the
        max_tokens cap (real output is usually shorter; the estimate
        skews high which is what we want for budget gating).
        """
        cost = (
            input_tokens * self._r_input / 1_000_000.0
            + max_output_tokens * self._r_output / 1_000_000.0
        )
        if web_search:
            n = max(0, int(web_search_calls or 1))
            cost += n * self._r_websearch
        return cost

    def cost_from_usage(
        self,
        usage: Any,
        *,
        web_search: bool = False,
        web_search_calls: int = 0,
        cache_write_ttl: str = "5m",
    ) -> float:
        """Compute actual USD cost from an Anthropic response.usage block.

        `usage` may be the SDK's Usage object or a dict with the same
        fields. Missing fields default to 0 — partial responses still
        produce a sensible (lower-bound) cost.

        web_search_calls tells us how many web_search invocations the
        model used; we don't pull this from `usage` because the SDK
        doesn't expose it directly and it's deterministic from the
        max_uses we passed in.
        """
        if usage is None:
            return 0.0

        def get(name: str) -> int:
            if isinstance(usage, dict):
                v = usage.get(name)
            else:
                v = getattr(usage, name, None)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        input_tokens = get("input_tokens")
        output_tokens = get("output_tokens")
        cache_read = get("cache_read_input_tokens")
        cache_write = get("cache_creation_input_tokens")

        cost = (
            input_tokens * self._r_input / 1_000_000.0
            + output_tokens * self._r_output / 1_000_000.0
            + cache_read * self._r_cache_read / 1_000_000.0
        )
        if cache_write:
            rate = (
                self._r_cache_write_1h
                if str(cache_write_ttl).lower() == "1h"
                else self._r_cache_write_5m
            )
            cost += cache_write * rate / 1_000_000.0
        if web_search:
            n = max(0, int(web_search_calls or 1))
            cost += n * self._r_websearch
        return cost

    # ── Budget guards ──────────────────────────────────────────────────

    @property
    def daily_budget_usd(self) -> float:
        return self._daily_budget

    async def today_total_usd(self) -> float:
        """Return current accumulated USD spend for today (UTC)."""
        try:
            return await self._store.get_daily_cost_total(today_utc())
        except Exception as exc:
            logger.warning("[cost] today_total_usd read failed: %s", exc)
            return 0.0

    async def can_spend(self, projected_usd: float) -> bool:
        """Return True iff we can afford `projected_usd` more today.

        Threshold is 99% of the daily budget so the boundary case
        (projection exactly equals remaining) clears. When False, the
        engine path should silently fall back to cached/empty rather
        than raise.

        On the *first* rejection of a given UTC day we also fire a
        critical alert via the in-process emitter — the engine pausing
        is "working as designed but you should know" territory. Dedup
        is keyed on the UTC date so repeat rejections within the same
        day stay silent; a new day re-arms the alert.
        """
        try:
            total = await self.today_total_usd()
        except Exception:
            return True  # fail-open on read errors; the actual call can still error
        ceiling = self._daily_budget * 0.99
        if (total + max(0.0, float(projected_usd))) > ceiling:
            logger.info(
                "[cost] BUDGET EXHAUSTED — total=$%.4f budget=$%.2f "
                "projected=$%.4f — engine paused for the day",
                total, self._daily_budget, projected_usd,
            )
            # First-rejection-of-the-day alert. Best-effort: the emitter
            # swallows its own failures, but we belt-and-brace here too
            # so the engine path can never raise from a budget rejection.
            try:
                day = today_utc()
                try:
                    calls = await self._store.get_daily_cost_calls(day)
                except Exception:
                    calls = 0
                from app.services.alert_emitter import emit_critical
                emit_critical(
                    title="pulse cost tripwire fired",
                    body=(
                        f"Daily LLM budget exhausted: spent "
                        f"${total:.2f}/{self._daily_budget:.2f} across "
                        f"{int(calls)} calls. Engine paused for the day; "
                        f"resumes at UTC midnight."
                    ),
                    dedup_key=f"tripwire-{day}",
                )
            except Exception as exc:
                logger.warning("[cost] tripwire alert dispatch failed: %s", exc)
            return False
        return True

    async def record_call(
        self,
        *,
        model: str,
        kind: str,
        cost_usd: float,
    ) -> None:
        """Persist an actual call cost to the daily counter.

        Writes BOTH the per-day total (`add_daily_cost`) and the per-kind
        bucket (`add_daily_cost_by_kind`). The per-kind write is
        fail-open: if it raises (e.g. fresh DB without the bucket table
        on a stale aiosqlite connection cache), we log a warning and
        continue rather than blocking the engine path.

        `kind` is a free-form bucket label (`news_scout`, `rewrite`,
        `storyline_scout`, `boot_scout`, etc.). When the
        `_kind_override` contextvar is set (boot path stamps
        `"boot_scout"`), we use the override for the by-kind row but
        leave the total path untouched. Empty/None kind defaults to
        `"unknown"` so we never write a NULL bucket.
        """
        usd = max(0.0, float(cost_usd or 0.0))
        try:
            await self._store.add_daily_cost(today_utc(), usd, calls_delta=1)
        except Exception as exc:
            logger.warning(
                "[cost] record_call write failed (model=%s kind=%s usd=$%.4f): %s",
                model, kind, usd, exc,
            )
            return

        # Per-kind aggregate write (PR feat/cost-by-kind-telemetry,
        # 2026-04-28). Best-effort: failures here MUST NOT regress the
        # daily-total path or break the engine.
        effective_kind = _kind_override.get() or (kind or "unknown")
        try:
            await self._store.add_daily_cost_by_kind(
                today_utc(), effective_kind, usd, calls_delta=1,
            )
        except Exception as exc:
            logger.warning(
                "[cost] by-kind write failed (kind=%s usd=$%.4f): %s",
                effective_kind, usd, exc,
            )

        logger.debug(
            "[cost] record model=%s kind=%s (effective=%s) usd=$%.4f",
            model, kind, effective_kind, usd,
        )
        # 80% budget alert removed 2026-04-28 — pulse-ops-bot now owns the alert ladder ($1/$2/$2.95).

    async def reset_if_new_day(self) -> None:
        """No-op for SQLite-backed bucket — `today_utc()` keys the row.

        Kept on the API surface so the contract matches the spec; rolling
        over to a new UTC day naturally reads 0 from the new key and the
        previous day's row stays intact for the 7-day admin view.
        """
        return None

    async def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot suitable for /admin/rerun/status."""
        try:
            total = await self.today_total_usd()
            calls = await self._store.get_daily_cost_calls(today_utc())
        except Exception:
            total = 0.0
            calls = 0
        budget = self._daily_budget
        remaining = max(0.0, budget - total)
        pct = (100.0 * total / budget) if budget > 0 else 0.0
        return {
            "day_utc": today_utc(),
            "total_usd": round(total, 4),
            "budget_usd": round(budget, 4),
            "remaining_usd": round(remaining, 4),
            "calls": int(calls),
            "percent_used": round(pct, 2),
        }
