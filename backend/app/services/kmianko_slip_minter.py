"""Kmianko bet-slip minter — turns a list of Rogue selection IDs into a
server-minted 6-char `bscode` that the operator's iframe recognises as a
fully-restored bet slip.

Context: PR #36 shipped a `?selectionId=<id>` deep link for Apuesta Total's
BTI-powered sportsbook. It half-works for singles but can't restore a full
BetBuilder (the iframe drops the piped 0VS id after boot) and can't populate
multi-leg combos (only the first selection is picked up). The correct
mechanism is the operator-side `share-betslip` endpoint which returns a
short code the iframe resolves into the complete slip.

Tokens are inlined in the spbkv3 HTML as anonymous JWTs — no login required.
We cache them for ~20h and the minted bscodes in-memory for 2h, keyed on a
deterministic hash of the sorted selection-id tuple.

Fails gracefully: any error returns None so the caller can fall back to the
PR #36 `selectionId` URL. Publishing never blocks on mint.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("pulse.kmianko_minter")


# Match the inlined JS assignment `APP_USER_DATA = {...}` in the spbkv3 HTML.
# The object is a single-line object literal; we only need the two JWT fields
# and their `expiredDate` decodes are handled separately.
_APP_USER_DATA_RE = re.compile(
    r"APP_USER_DATA\s*=\s*(\{.*?\})\s*;", re.DOTALL,
)
_INTERNAL_TOKEN_RE = re.compile(r"['\"]internalToken['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_SESSION_TOKEN_RE = re.compile(r"['\"]sessionToken['\"]\s*:\s*['\"]([^'\"]+)['\"]")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_expires_at(jwt: str) -> Optional[float]:
    """Return the JWT's `expiredDate` claim as unix seconds, or None.

    Kmianko stores `expiredDate` in **milliseconds** (not standard `exp`
    seconds). We convert here so callers can compare to `time.time()`.
    Falls back to the `exp` claim when `expiredDate` is absent.
    """
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        exp_ms = payload.get("expiredDate")
        if isinstance(exp_ms, (int, float)) and exp_ms > 0:
            return float(exp_ms) / 1000.0
        exp_s = payload.get("exp")
        if isinstance(exp_s, (int, float)) and exp_s > 0:
            return float(exp_s)
    except Exception:
        return None
    return None


class KmiankoSlipMinter:
    """Mints `bscode` slip codes via Kmianko's share-betslip endpoint.

    Thread-safe-ish: httpx AsyncClient is shared, a single asyncio.Semaphore
    caps concurrency, and a single asyncio.Lock guards token refresh.
    """

    # Cache a minted bscode for this long. Slip contents are stable — a
    # given (sorted) set of selection IDs always mints the same code for
    # our purposes within this window.
    _BSCODE_TTL_S = 2 * 3600
    # Refresh tokens when the session JWT is within this of expiry, or when
    # our cached fetch is older than this — whichever comes first. 20h
    # matches the ~24h session lifetime Kmianko issues (see module docstring).
    _TOKEN_REFRESH_WINDOW_S = 3600
    _TOKEN_MAX_AGE_S = 20 * 3600

    def __init__(
        self,
        base_url: str,
        spbkv3_path: str,
        *,
        timeout: float = 10.0,
        concurrency: int = 5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._spbkv3_path = spbkv3_path if spbkv3_path.startswith("/") else "/" + spbkv3_path
        self._client = httpx.AsyncClient(timeout=timeout)
        self._sem = asyncio.Semaphore(concurrency)
        self._token_lock = asyncio.Lock()

        # Token state
        self._internal: Optional[str] = None
        self._session: Optional[str] = None
        self._token_fetched_at: float = 0.0
        self._session_exp: Optional[float] = None

        # bscode cache: {key: (bscode, minted_at)}
        self._bscode_cache: dict[str, tuple[str, float]] = {}

    # ── Token handling ────────────────────────────────────────────────

    def _tokens_fresh(self) -> bool:
        if not self._internal or not self._session:
            return False
        age = time.time() - self._token_fetched_at
        if age > self._TOKEN_MAX_AGE_S:
            return False
        if self._session_exp is not None:
            if self._session_exp - time.time() < self._TOKEN_REFRESH_WINDOW_S:
                return False
        return True

    async def _fetch_tokens_locked(self) -> None:
        url = f"{self._base_url}{self._spbkv3_path}"
        logger.info("[kmianko] fetching spbkv3 HTML for tokens: %s", url)
        try:
            r = await self._client.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "Mozilla/5.0 (compatible; PulsePOC/1.0)",
                },
            )
        except Exception as exc:
            logger.warning("[kmianko] token fetch failed: %s", exc)
            return
        if r.status_code != 200:
            logger.warning("[kmianko] token fetch non-200: %s", r.status_code)
            return
        html = r.text
        m_internal = _INTERNAL_TOKEN_RE.search(html)
        m_session = _SESSION_TOKEN_RE.search(html)
        if not m_internal or not m_session:
            logger.warning(
                "[kmianko] token extraction failed — internal=%s session=%s",
                bool(m_internal), bool(m_session),
            )
            return
        self._internal = m_internal.group(1)
        self._session = m_session.group(1)
        self._token_fetched_at = time.time()
        self._session_exp = _jwt_expires_at(self._session)
        logger.info(
            "[kmianko] tokens refreshed — session exp %s (%.1fh)",
            self._session_exp,
            (self._session_exp - time.time()) / 3600 if self._session_exp else -1,
        )

    async def get_tokens(self) -> tuple[Optional[str], Optional[str]]:
        async with self._token_lock:
            if not self._tokens_fresh():
                await self._fetch_tokens_locked()
            return self._internal, self._session

    async def _refresh_tokens(self) -> tuple[Optional[str], Optional[str]]:
        """Force a token refresh (used after 401/403)."""
        async with self._token_lock:
            await self._fetch_tokens_locked()
            return self._internal, self._session

    # ── Minting ────────────────────────────────────────────────────────

    def _cache_key(self, selection_ids: list[str]) -> str:
        joined = "|".join(sorted(s for s in selection_ids if s))
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    async def mint(self, selection_ids: list[str]) -> Optional[str]:
        """Mint a bscode for the given selection IDs. Returns None on any
        failure (caller should fall back to selectionId deep-link).
        """
        if not selection_ids:
            return None
        # Drop empties defensively; the endpoint 400s on a list with "".
        clean = [s for s in selection_ids if s]
        if not clean:
            return None

        key = self._cache_key(clean)
        now = time.time()
        cached = self._bscode_cache.get(key)
        if cached and (now - cached[1]) < self._BSCODE_TTL_S:
            return cached[0]

        async with self._sem:
            bscode = await self._mint_once(clean)
            if bscode is None:
                return None
            self._bscode_cache[key] = (bscode, time.time())
            # Opportunistic cache trim — keep at most ~2000 entries so we
            # don't balloon memory across days of reruns.
            if len(self._bscode_cache) > 2000:
                # Drop the oldest 500.
                drop = sorted(self._bscode_cache.items(), key=lambda kv: kv[1][1])[:500]
                for k, _ in drop:
                    self._bscode_cache.pop(k, None)
            return bscode

    async def _mint_once(self, selection_ids: list[str]) -> Optional[str]:
        internal, session = await self.get_tokens()
        if not internal or not session:
            logger.warning("[kmianko] mint aborted — no tokens")
            return None
        body = {"selections": selection_ids}
        url = f"{self._base_url}/api/betslip/betslip/share-betslip"
        headers = {
            "Authorization": internal,
            "Session": session,
            "Content-Type": "application/json",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}{self._spbkv3_path}",
            "Accept": "*/*",
        }
        try:
            r = await self._client.post(url, headers=headers, json=body)
        except Exception as exc:
            logger.warning(
                "[kmianko] mint POST failed (n=%d): %s", len(selection_ids), exc,
            )
            return None

        if r.status_code in (401, 403):
            logger.info(
                "[kmianko] mint got %s — refreshing tokens + retrying once",
                r.status_code,
            )
            internal, session = await self._refresh_tokens()
            if not internal or not session:
                return None
            headers["Authorization"] = internal
            headers["Session"] = session
            try:
                r = await self._client.post(url, headers=headers, json=body)
            except Exception as exc:
                logger.warning("[kmianko] mint retry failed: %s", exc)
                return None

        if r.status_code not in (200, 201):
            logger.warning(
                "[kmianko] mint non-2xx: %s body=%r selections=%d",
                r.status_code, r.text[:200], len(selection_ids),
            )
            return None

        bscode = (r.text or "").strip().strip('"')
        # Sanity: real bscodes are 6 alphanumeric chars. Anything else is
        # suspicious — log and bail so we don't ship a garbage URL.
        if not re.fullmatch(r"[A-Za-z0-9]{4,12}", bscode):
            logger.warning("[kmianko] mint returned unexpected body: %r", bscode)
            return None
        return bscode

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
