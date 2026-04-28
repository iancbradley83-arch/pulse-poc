"""
Async HTTP client for the Pulse backend.

Methods:
  health()       -> dict  (e.g. {"ok": True})
  cost(days=1)   -> dict  (e.g. {"days": [...], "total_usd": 0.42, "total_calls": 12})
  feed()         -> dict  (e.g. {"count": 108, "cards": [...]})

All methods raise PulseError on network failure or unexpected response.
Basic auth is added only for /admin/* endpoints.
"""
import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0


class PulseError(Exception):
    """Raised when a Pulse request fails or returns an unexpected response."""


class PulseClient:
    def __init__(
        self,
        base_url: str,
        admin_user: str = "",
        admin_pass: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_auth = (admin_user, admin_pass) if admin_user else None
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> Dict[str, Any]:
        """GET /health — no auth required."""
        url = f"{self._base_url}/health"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            raise PulseError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise PulseError(f"http {exc.response.status_code}") from exc
        except Exception as exc:
            raise PulseError(f"request failed: {exc}") from exc

    async def cost(self, days: int = 1) -> Dict[str, Any]:
        """GET /admin/cost.json?days=N — basic auth required if credentials set."""
        url = f"{self._base_url}/admin/cost.json"
        params = {"days": days}
        try:
            kwargs: Dict[str, Any] = {"params": params}
            if self._admin_auth:
                kwargs["auth"] = self._admin_auth
            resp = await self._client.get(url, **kwargs)
            resp.raise_for_status()
            data = resp.json()
            return self._normalise_cost(data, days)
        except httpx.TimeoutException as exc:
            raise PulseError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise PulseError(f"http {exc.response.status_code}") from exc
        except PulseError:
            raise
        except Exception as exc:
            raise PulseError(f"request failed: {exc}") from exc

    def _normalise_cost(self, data: Dict[str, Any], days: int) -> Dict[str, Any]:
        """
        Normalise the /admin/cost response into a consistent shape:
          {
            "total_usd": float,
            "total_calls": int,
            "days": [
              {"date": "YYYY-MM-DD", "usd": float, "calls": int, "limit_usd": float}
            ]
          }

        The Pulse endpoint may return different shapes depending on version; we
        handle gracefully and log warnings rather than crashing.
        """
        try:
            # Preferred shape: {"today": {"usd": X, "calls": N}, "days": [...]}
            if "today" in data:
                today = data["today"]
                total_usd = float(today.get("usd", 0.0))
                total_calls = int(today.get("calls", 0))
            elif "total_usd" in data:
                total_usd = float(data["total_usd"])
                total_calls = int(data.get("total_calls", 0))
            elif "usd" in data:
                total_usd = float(data["usd"])
                total_calls = int(data.get("calls", 0))
            else:
                logger.warning("cost response has unexpected shape: %s", data)
                total_usd = 0.0
                total_calls = 0

            day_rows = data.get("days", [])
            limit_usd = float(data.get("limit_usd", 3.0))

            normalised_days = []
            for row in day_rows:
                normalised_days.append(
                    {
                        "date": row.get("date", ""),
                        "usd": float(row.get("usd", 0.0)),
                        "calls": int(row.get("calls", 0)),
                        "limit_usd": float(row.get("limit_usd", limit_usd)),
                    }
                )

            return {
                "total_usd": total_usd,
                "total_calls": total_calls,
                "days": normalised_days,
                "limit_usd": limit_usd,
            }
        except Exception as exc:
            raise PulseError(f"couldn't parse cost response: {exc}") from exc

    async def cost_detail(self) -> Dict[str, Any]:
        """
        GET /admin/cost.json?detail=1 — enriched payload from PR #84.

        Shape (live as of 2026-04-28):
          {
            "total_usd": float, "total_calls": int, "limit_usd": float,
            "days": [...],
            "by_kind": {kind: {"usd": float, "calls": int}} or {},
            "cards_in_feed_now": int|null,
            "unique_cards_published_today": int|null,
            "republish_events_today": int|null,
            "rewrite_cache_hits_today": int|null
          }

        The enrichment fields may be null while the new telemetry warms up.
        Caller renders missing fields gracefully.
        """
        url = f"{self._base_url}/admin/cost.json"
        params = {"detail": 1}
        try:
            kwargs: Dict[str, Any] = {"params": params}
            if self._admin_auth:
                kwargs["auth"] = self._admin_auth
            resp = await self._client.get(url, **kwargs)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise PulseError("cost_detail: response was not an object")
            return data
        except httpx.TimeoutException as exc:
            raise PulseError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise PulseError(f"http {exc.response.status_code}") from exc
        except PulseError:
            raise
        except Exception as exc:
            raise PulseError(f"request failed: {exc}") from exc

    async def embeds(self) -> Dict[str, Any]:
        """GET /admin/embeds.json — basic auth required if credentials set."""
        url = f"{self._base_url}/admin/embeds.json"
        try:
            kwargs: Dict[str, Any] = {}
            if self._admin_auth:
                kwargs["auth"] = self._admin_auth
            resp = await self._client.get(url, **kwargs)
            if resp.status_code == 404:
                raise PulseError("404")
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            raise PulseError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise PulseError(f"http {exc.response.status_code}") from exc
        except PulseError:
            raise
        except Exception as exc:
            raise PulseError(f"request failed: {exc}") from exc

    async def feed(self) -> Dict[str, Any]:
        """GET /api/feed — no auth required."""
        url = f"{self._base_url}/api/feed"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Normalise to {"count": N, "cards": [...]}
            cards = data.get("cards", data if isinstance(data, list) else [])
            return {"count": len(cards), "cards": cards}
        except httpx.TimeoutException as exc:
            raise PulseError("unreachable") from exc
        except httpx.HTTPStatusError as exc:
            raise PulseError(f"http {exc.response.status_code}") from exc
        except PulseError:
            raise
        except Exception as exc:
            raise PulseError(f"request failed: {exc}") from exc
