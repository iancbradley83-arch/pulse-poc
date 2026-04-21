"""Async client for Apuesta Total's bet-slip pricing endpoints.

Why a separate client (instead of folding it into RogueClient): kmianko.com is
a different host with a different auth scheme (`session` header carrying an
HS256 JWT minted by the kmianko bootstrap, not the msjxk Rogue config-JWT
flow). Different rate limits, different lifecycle, different failure modes.

What it does:

  - `quote_bet_builder(virtual_selection_id)` → real correlated decimal odds
    for a single-event Bet Builder, plus per-leg breakdowns. The
    `virtual_selection_id` is the `VirtualSelection` field returned by Rogue's
    `/v1/sportsdata/betbuilder/match` validator (looks like `0VS<piped-leg-ids>`).

  - `quote_combo(selection_ids)` → real cross-event accumulator decimal odds.
    Math = naive product × (1 + applicable_combo_bonus_pct). The bonus is
    applied by the operator only if every leg is eligible for the same bonus,
    every leg meets `MinimumSelectionOdds` (default 1.5), and the leg count
    falls in the bonus's `Rules` map (3+ legs).

  - `fetch_combo_bonuses()` → cached snapshot of `/api/betslip/combo-bonus/bonuses`.
    Auto-refreshed every `bonus_ttl_seconds`.

Failure modes handled:
  - 403 `"token expected"` → invalidate session, retry once
  - Network errors → propagate (caller decides whether to fall back)
  - Malformed responses → return None, log

Not handled (intentional):
  - Live odds drift between quote and bet placement — Pulse doesn't place
    bets; final price is whatever the user sees in the slip.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import quote as urlquote

import httpx

from app.services.kmianko_session import KmiankoSession, KmiankoSessionError

logger = logging.getLogger(__name__)

KMIANKO_HOST = "https://prod20392.kmianko.com"

# Match the Playwright-minted UA so server-side fingerprinting (if any) sees a
# consistent client across mint and use.
_DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "origin": KMIANKO_HOST,
    "referer": f"{KMIANKO_HOST}/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.5 Safari/605.1.15"
    ),
}


class KmiankoBetslipError(RuntimeError):
    pass


class KmiankoBetslipClient:
    """Async client. Use as `async with KmiankoBetslipClient(session) as c: ...`
    or call `.close()` manually."""

    def __init__(
        self,
        session: KmiankoSession,
        *,
        timeout_s: float = 20.0,
        bonus_ttl_seconds: int = 3600,
    ):
        self._session = session
        self._http = httpx.AsyncClient(
            base_url=KMIANKO_HOST,
            headers=_DEFAULT_HEADERS,
            timeout=timeout_s,
            http2=True,
        )
        self._bonus_ttl = bonus_ttl_seconds
        self._bonuses_cache: Optional[list[dict[str, Any]]] = None
        self._bonuses_fetched_at: float = 0.0
        self._bonuses_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "KmiankoBetslipClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Low-level request with single 403-retry ──

    async def _request_with_session(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Any = None,
    ) -> httpx.Response:
        token = await self._session.get_token()
        cookies = await self._session.get_cookies()
        headers = {"session": token}
        if body is not None:
            headers["content-type"] = "application/json"
        res = await self._http.request(
            method, path, params=params,
            content=json.dumps(body) if body is not None else None,
            headers=headers, cookies=cookies,
        )
        if res.status_code == 403:
            # Either the session JWT or the CF clearance cookie has expired —
            # refresh both via a fresh mint, retry once.
            try:
                token = await self._session.get_token(force_refresh=True)
                cookies = await self._session.get_cookies()
            except KmiankoSessionError:
                return res  # let caller see the original 403
            headers["session"] = token
            res = await self._http.request(
                method, path, params=params,
                content=json.dumps(body) if body is not None else None,
                headers=headers, cookies=cookies,
            )
        return res

    # ── Combo bonus catalogue ──

    async def fetch_combo_bonuses(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return the operator's combo-bonus catalogue. Cached per `bonus_ttl_seconds`."""
        if (
            not force_refresh
            and self._bonuses_cache is not None
            and (time.time() - self._bonuses_fetched_at) < self._bonus_ttl
        ):
            return self._bonuses_cache
        async with self._bonuses_lock:
            if (
                not force_refresh
                and self._bonuses_cache is not None
                and (time.time() - self._bonuses_fetched_at) < self._bonus_ttl
            ):
                return self._bonuses_cache
            res = await self._request_with_session("GET", "/api/betslip/combo-bonus/bonuses")
            if res.status_code != 200:
                logger.warning(
                    "[KmiankoBetslip] combo-bonus fetch failed: %s %s",
                    res.status_code, res.text[:200],
                )
                self._bonuses_cache = []
            else:
                try:
                    self._bonuses_cache = res.json().get("ComboBonuses", []) or []
                except Exception as exc:
                    logger.warning("[KmiankoBetslip] combo-bonus parse failed: %s", exc)
                    self._bonuses_cache = []
            self._bonuses_fetched_at = time.time()
            return self._bonuses_cache

    # ── BB pricing ──

    async def quote_bet_builder(
        self, virtual_selection_id: str,
    ) -> Optional[dict[str, Any]]:
        """Quote a same-event Bet Builder.

        Args:
            virtual_selection_id: The `VirtualSelection` value returned by
                Rogue's `/v1/sportsdata/betbuilder/match`. Looks like
                `0VS<leg1>|<leg2>|<leg3>...`.

        Returns:
            { decimal: float, american: str, fractional: str,
              leg_decimals: list[float], raw: <full slice> }
            or None on failure (caller falls back to naive product).
        """
        body = [{
            "selectionId": virtual_selection_id,
            "viewKey": 1,
            "isCrossBet": False,
            "isDynamicMarket": False,
            "isBetBuilderBet": True,
            "promotionIds": [],
            "returnCombinableSelections": False,
        }]
        try:
            res = await self._request_with_session(
                "POST", "/api/betslip/betslip/anonymous", body=body,
            )
        except Exception as exc:
            logger.warning("[KmiankoBetslip] BB POST errored: %s", exc)
            return None

        if res.status_code != 201:
            logger.info(
                "[KmiankoBetslip] BB quote rejected %s for %s: %s",
                res.status_code, virtual_selection_id, res.text[:200],
            )
            return None

        try:
            data = res.json()
        except Exception as exc:
            logger.warning("[KmiankoBetslip] BB response parse failed: %s", exc)
            return None

        # Response is a list of items (one per slip line). For a single BB we
        # send one item, get one back.
        if not isinstance(data, list) or not data:
            logger.warning("[KmiankoBetslip] BB response not a non-empty list")
            return None

        item = data[0]
        sel = (item or {}).get("selection") or {}
        cs = sel.get("Changeset") or {}
        true_odds = cs.get("TrueOdds")
        disp = cs.get("DisplayOdds") or {}
        if not isinstance(true_odds, (int, float)) or true_odds <= 1.0:
            logger.warning("[KmiankoBetslip] BB response missing valid TrueOdds")
            return None

        leg_decimals: list[float] = []
        for s in (cs.get("Selections") or []):
            t = s.get("TrueOdds")
            if isinstance(t, (int, float)) and t > 1.0:
                leg_decimals.append(float(t))

        return {
            "decimal": float(true_odds),
            "american": str(disp.get("American", "")) or None,
            "fractional": str(disp.get("Fractional", "")) or None,
            "leg_decimals": leg_decimals,
            "raw_changeset": cs,
        }

    # ── Combo pricing ──

    async def quote_combo(
        self, selection_ids: list[str],
    ) -> Optional[dict[str, Any]]:
        """Quote a cross-event accumulator with operator combo-bonus applied.

        Args:
            selection_ids: 2+ leg selection IDs from different events.

        Returns:
            { decimal: float, naive_product: float, bonus_pct: float,
              bonus_id: str|None, leg_decimals: list[float],
              eligible_for_bonus: bool, reason: str }
            or None on failure.
        """
        if len(selection_ids) < 2:
            return None

        body = [
            {
                "selectionId": sid,
                "viewKey": 1,
                "isCrossBet": True,
                "isDynamicMarket": False,
                "isBetBuilderBet": False,
                "promotionIds": [],
                "returnCombinableSelections": False,
            }
            for sid in selection_ids
        ]
        try:
            res = await self._request_with_session(
                "POST", "/api/betslip/betslip/anonymous", body=body,
            )
        except Exception as exc:
            logger.warning("[KmiankoBetslip] combo POST errored: %s", exc)
            return None

        if res.status_code != 201:
            logger.info(
                "[KmiankoBetslip] combo quote rejected %s: %s",
                res.status_code, res.text[:200],
            )
            return None

        try:
            data = res.json()
        except Exception:
            return None
        if not isinstance(data, list) or len(data) != len(selection_ids):
            logger.warning(
                "[KmiankoBetslip] combo response item count mismatch: got %d want %d",
                len(data) if isinstance(data, list) else -1, len(selection_ids),
            )
            return None

        leg_meta: list[dict[str, Any]] = []
        for it in data:
            mkt = (it or {}).get("market") or {}
            cs = mkt.get("Changeset") or {}
            sel = cs.get("Selection") or {}
            t = sel.get("TrueOdds")
            mt = (sel.get("MarketType") or {}).get("_id") or cs.get("MarketType", {}).get("_id")
            if not isinstance(t, (int, float)) or t <= 1.0:
                logger.warning("[KmiankoBetslip] combo leg missing TrueOdds")
                return None
            leg_meta.append({
                "true_odds": float(t),
                "market_type_id": mt,
                "sport_id": cs.get("SportId"),
                "master_league_id": cs.get("MasterLeagueId") or cs.get("LeagueId"),
                "combo_bonus_ids": cs.get("ComboBonuses") or [],
                "is_live": bool(cs.get("IsLive", False)),
            })

        leg_decimals = [m["true_odds"] for m in leg_meta]
        naive = 1.0
        for d in leg_decimals:
            naive *= d

        bonus_pct, bonus_id, reason = await self._compute_combo_bonus(leg_meta)
        decimal = round(naive * (1.0 + bonus_pct), 4)

        return {
            "decimal": decimal,
            "naive_product": round(naive, 4),
            "bonus_pct": bonus_pct,
            "bonus_id": bonus_id,
            "leg_decimals": leg_decimals,
            "eligible_for_bonus": bonus_pct > 0,
            "reason": reason,
        }

    async def _compute_combo_bonus(
        self, leg_meta: list[dict[str, Any]],
    ) -> tuple[float, Optional[str], str]:
        """Determine the combo-bonus multiplier (additive %) for a set of legs.

        Returns (bonus_pct, bonus_id, reason). bonus_pct is 0.0 when no bonus
        applies (combo gets pure naive product).
        """
        n = len(leg_meta)
        if n < 3:
            return 0.0, None, f"only {n} legs (bonus needs ≥3)"

        # Intersect leg-eligible bonuses — every leg must list the bonus to
        # qualify.
        eligible_sets = [set(m["combo_bonus_ids"]) for m in leg_meta]
        common = set.intersection(*eligible_sets) if eligible_sets else set()
        if not common:
            return 0.0, None, "no bonus shared by all legs"

        bonuses = await self.fetch_combo_bonuses()
        bonus_by_id = {b.get("_id"): b for b in bonuses}

        # Try each common bonus; pick the one giving the highest applicable %.
        best_pct = 0.0
        best_id: Optional[str] = None
        best_reason = "no bonus matched constraints"
        for bid in common:
            b = bonus_by_id.get(bid)
            if not b:
                continue
            min_sel = float(b.get("MinimumSelectionOdds") or 0)
            if min_sel and any(m["true_odds"] < min_sel for m in leg_meta):
                continue
            rules = b.get("Rules") or {}
            # Rules are a string-keyed map of leg_count → bonus_pct (decimal,
            # e.g. "0.05"). Cap at the highest defined entry if leg count
            # exceeds keys.
            int_keys = sorted((int(k) for k in rules.keys() if str(k).isdigit()))
            if not int_keys:
                continue
            applicable = max((k for k in int_keys if k <= n), default=int_keys[0])
            try:
                pct = float(rules[str(applicable)])
            except Exception:
                continue
            if pct > best_pct:
                best_pct = pct
                best_id = bid
                best_reason = (
                    f"bonus {b.get('Name','?')!r}: {n} legs → +{pct*100:.0f}% "
                    f"(min_sel={min_sel}, applicable_rule={applicable})"
                )

        return best_pct, best_id, best_reason
