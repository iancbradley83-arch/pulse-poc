"""Rogue API client — Python port of the Rogue MCP TypeScript wrapper.

Mirrors the patterns in `rogue-api-mcp/src/index.ts`:
  - Anonymous JWT auth with auto-refresh 5 minutes before expiry.
  - Token-bucket rate limiter (default 5 req/s).
  - 401 retry-once with invalidated session.
  - Event name normalisation (multilanguage `{EN: "..."}` objects flattened).
  - 204 handling (settled/ended single events return null).

Used by the catalogue loader at boot and (later stages) by the live pulse
pipeline. Read-only — no bet placement, no customer session actions.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class RogueAuthError(RuntimeError):
    pass


class RogueApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Rogue API error {status}: {message}")
        self.status = status


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


class RogueAuth:
    """Anonymous JWT flow. Exp is in milliseconds (Rogue quirk #4)."""

    def __init__(self, base_url: str, config_jwt: str, http: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._config_jwt = config_jwt
        self._http = http
        self._session_jwt: Optional[str] = None
        self._expiry_ms: Optional[int] = None
        self._lock = asyncio.Lock()

    def _expiring_soon(self) -> bool:
        if self._expiry_ms is None:
            return True
        return (time.time() * 1000) >= (self._expiry_ms - 5 * 60 * 1000)

    async def get_session_jwt(self) -> str:
        if self._session_jwt and not self._expiring_soon():
            return self._session_jwt
        async with self._lock:
            if self._session_jwt and not self._expiring_soon():
                return self._session_jwt
            await self._authenticate()
            assert self._session_jwt is not None
            return self._session_jwt

    async def _authenticate(self) -> None:
        if not self._config_jwt:
            raise RogueAuthError("ROGUE_CONFIG_JWT is not set")
        url = f"{self._base_url}/v1/auth/anonymous"
        res = await self._http.get(
            url,
            params={"configurationJWT": self._config_jwt},
            headers={"Accept": "application/json"},
        )
        if res.status_code != 200:
            raise RogueAuthError(f"Auth failed: {res.status_code} {res.text[:200]}")
        data = res.json()
        jwt = data.get("jwt")
        if not jwt:
            raise RogueAuthError(f"Auth response missing jwt: {data}")
        self._session_jwt = jwt
        try:
            payload_b64 = jwt.split(".")[1]
            payload = json.loads(_b64url_decode(payload_b64).decode())
            # Rogue quirk #4: Exp is in milliseconds, not seconds.
            self._expiry_ms = int(payload.get("Exp") or (time.time() * 1000 + 3600_000))
        except Exception:
            self._expiry_ms = int(time.time() * 1000 + 3600_000)

    async def headers(self) -> dict[str, str]:
        jwt = await self.get_session_jwt()
        return {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}

    def invalidate(self) -> None:
        self._session_jwt = None
        self._expiry_ms = None


class RateLimiter:
    """Token-bucket limiter. Mirrors the MCP's 5 req/s default."""

    def __init__(self, per_second: float):
        self._max = per_second
        self._tokens = per_second
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait = (1 - self._tokens) / self._max
        await asyncio.sleep(wait)
        async with self._lock:
            self._refill()
            self._tokens = max(0.0, self._tokens - 1)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max, self._tokens + elapsed * self._max)
        self._last_refill = now


def _normalize_name(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict) and "EN" in val:
        return str(val["EN"])
    if val is None:
        return ""
    return str(val)


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    # Flatten multilanguage name fields. Mirrors normalizeEvent in the MCP.
    out = dict(event)
    for key in ("EventName", "SportName", "LeagueName", "RegionName"):
        if key in out:
            out[key] = _normalize_name(out[key])
    return out


class RogueClient:
    """Async wrapper around the Rogue sportsbook API.

    Usage:
        client = RogueClient(base_url, config_jwt)
        try:
            await client.health()
            events = await client.get_all_events(sport_ids="1", is_top_league=True)
        finally:
            await client.close()
    """

    def __init__(
        self,
        base_url: str,
        config_jwt: str,
        per_second: float = 5.0,
        timeout_s: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout_s)
        self._auth = RogueAuth(self._base_url, config_jwt, self._http)
        self._limiter = RateLimiter(per_second)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RogueClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        requires_auth: bool = True,
    ) -> Any:
        await self._limiter.acquire()
        clean_params = {k: _coerce_param(v) for k, v in (params or {}).items() if v is not None}
        headers = await self._auth.headers() if requires_auth else {"Accept": "application/json"}
        url = f"{self._base_url}{path}"

        res = await self._http.get(url, params=clean_params, headers=headers)

        if res.status_code == 401 and requires_auth:
            self._auth.invalidate()
            retry_headers = await self._auth.headers()
            res = await self._http.get(url, params=clean_params, headers=retry_headers)

        if res.status_code == 204:
            return None
        if res.status_code >= 400:
            raise RogueApiError(res.status_code, res.text[:400])
        return res.json()

    # ── Endpoints ──

    async def health(self) -> Any:
        return await self._request("/health", requires_auth=False)

    async def get_sports(self, *, has_live: Optional[bool] = None, has_fixture: Optional[bool] = None, locale: str = "en") -> dict[str, Any]:
        return await self._request(
            "/v1/sportsdata/sports",
            {"hasLive": has_live, "hasFixture": has_fixture, "locale": locale},
        )

    async def get_leagues(
        self,
        *,
        sport_ids: Optional[str] = None,
        has_fixture: Optional[bool] = None,
        has_live: Optional[bool] = None,
        locale: str = "en",
        take: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "/v1/sportsdata/leagues",
            {
                "sportIDs": sport_ids,
                "hasFixture": has_fixture,
                "hasLive": has_live,
                "locale": locale,
                "take": take,
                "skip": skip,
            },
        )

    async def get_events(
        self,
        *,
        sport_ids: Optional[str] = None,
        league_ids: Optional[str] = None,
        event_type: Optional[str] = None,
        is_live: Optional[bool] = None,
        is_top_league: Optional[bool] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        include_markets: str = "none",
        locale: str = "en",
        take: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        data = await self._request(
            "/v1/sportsdata/multilanguage/events",
            {
                "sportIDs": sport_ids,
                "leagueIDs": league_ids,
                "eventType": event_type,
                "isLive": is_live,
                "isTopLeague": is_top_league,
                "fromDate": from_date,
                "toDate": to_date,
                "includeMarkets": include_markets,
                "locale": locale,
                "take": take,
                "skip": skip,
            },
        )
        if data and "Events" in data:
            data["Events"] = [_normalize_event(e) for e in data["Events"]]
        return data or {"Events": [], "TotalCount": 0}

    async def get_all_events(
        self,
        *,
        max_events: int = 500,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Auto-paginate `get_events` up to `max_events`."""
        take = 100
        skip = 0
        out: list[dict[str, Any]] = []
        kwargs.setdefault("include_markets", "none")
        while len(out) < max_events:
            page = await self.get_events(take=take, skip=skip, **kwargs)
            events = page.get("Events", [])
            if not events:
                break
            out.extend(events)
            total = page.get("TotalCount", len(out))
            if len(events) < take:
                break
            skip += take
            if skip >= total:
                break
        return out[:max_events]

    async def get_event(
        self,
        event_id: str,
        *,
        include_markets: str = "all",
        locale: str = "en",
    ) -> Optional[dict[str, Any]]:
        data = await self._request(
            "/v1/sportsdata/event",
            {"id": event_id, "includeMarkets": include_markets, "locale": locale},
        )
        if data is None:
            return None
        # Single-event endpoints sometimes wrap in { Event: ... } (v2 behaviour).
        if isinstance(data, dict) and "Event" in data and isinstance(data["Event"], dict):
            data = data["Event"]
        return _normalize_event(data)

    async def betbuilder_match(self, selection_ids: list[str]) -> Any:
        return await self._request(
            "/v1/sportsdata/betbuilder/match",
            {"selectionIDs": ",".join(selection_ids)},
        )

    async def calculate_bets(
        self,
        selection_ids: list[str],
        *,
        odds_style: str = "decimal",
        locale: str = "en",
        extended_additional_info: bool = False,
    ) -> Any:
        """Server-side bet pricing for any combination of selection IDs.

        Endpoint: POST /v1/betting/calculateBets (per the official OpenAPI spec
        at spec/openapi.json — security is the same anonymous Bearer JWT we
        already use elsewhere; no customer session required).

        Args:
            selection_ids: list of leg selection ids. For Bet Builders pass the
                single VirtualSelection id (`0VS<piped-leg-ids>`) returned by
                `betbuilder_match`. For cross-event combos pass each leg id.
            odds_style: 'decimal' | 'american' | 'fractional' | 'malay' | 'indo' | 'hk'
            locale: 'en' or 'es-pe'
            extended_additional_info: pulls richer per-leg breakdowns

        Returns the raw API response. Useful fields:
            data["Selections"][i]: per-leg `TrueOdds`, `DisplayOdds`, `BetslipLine`
            data["Bets"][i]: each candidate bet TYPE the selections support
                ("Single", "Combo", "BetBuilder", "System"), each with
                `TrueOdds`, `DisplayOdds`, `MaxStake`, `MinStake`, `ComboBonus`
                (`{Percent, Gain, ...}` — the operator's combo boost as a
                multiplicative %).
            data["Errors"][i]: combinability problems (e.g. "BetBuilderInvalid")

        This replaces the prior Kmianko bet-slip detour — the official Rogue
        API exposes the same prices natively, no Cloudflare bypass needed.
        """
        await self._limiter.acquire()
        body = {
            "Selections": [{"Id": sid} for sid in selection_ids],
            "OddsStyle": odds_style,
            "Locale": locale,
            "ExtendedAdditionalInfo": extended_additional_info,
        }
        headers = await self._auth.headers()
        headers["Content-Type"] = "application/json"
        url = f"{self._base_url}/v1/betting/calculateBets"
        res = await self._http.post(url, json=body, headers=headers)
        if res.status_code == 401:
            self._auth.invalidate()
            headers = await self._auth.headers()
            headers["Content-Type"] = "application/json"
            res = await self._http.post(url, json=body, headers=headers)
        if res.status_code == 204:
            return None
        if res.status_code >= 400:
            raise RogueApiError(res.status_code, res.text[:400])
        return res.json()


def _coerce_param(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v
