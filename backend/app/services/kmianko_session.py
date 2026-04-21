"""Kmianko session-token minter.

Apuesta Total's bet-slip pricing endpoints (`prod20392.kmianko.com/api/betslip/...`)
use a custom `session: <JWT>` header instead of the Rogue `Authorization: Bearer`
flow. The JWT is anonymous (`customerId: -1`) but **server-side stateful** — the
backend tracks the session in a store, so a freshly-minted JWT works while one
left dangling beyond its heartbeat window returns 403 `"token expected"`.

There is no public mint endpoint we can replicate via a plain HTTP client;
the page bootstrap does it during the initial HTML+JS load behind a Cloudflare
managed challenge. So we use Playwright (Chromium) to load any kmianko event
URL, watch outgoing API requests, and lift the `session` header off the first
one we see. The token's `expiredDate` claim runs ~24h, but in practice the
server invalidates ~minutes after the page closes — so we re-mint on demand
(every call cycle) and cache for a short TTL.

Used by `KmiankoBetslipClient` to call:
  - POST /api/betslip/betslip/anonymous            (BB / combo pricing quote)
  - GET  /api/betslip/betslip/updates/anonymous    (price polling)
  - GET  /api/betslip/combo-bonus/bonuses          (combo boost rules)

Costs ~3-6s per mint (Playwright cold-start + page load). With token TTL
defaulting to 10 minutes, an hourly news/BB pipeline will mint ~6×/hr.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# A real event URL on the operator (any kmianko spbkv3 page works — we only
# need the page to load so its JS fires the first authenticated API call.)
DEFAULT_BOOTSTRAP_URL = os.getenv(
    "KMIANKO_BOOTSTRAP_URL",
    "https://prod20392.kmianko.com/es-pe/spbkv3/F%C3%BAtbol/Inglaterra/"
    "Premier-League/Brighton-vs-Chelsea/830846853175410688",
)
# Cache TTL — keep it well under the empirically-observed server-side
# invalidation window. 10 minutes is a safe default; bump if the cost of
# minting becomes a concern.
DEFAULT_TTL_SECONDS = int(os.getenv("KMIANKO_SESSION_TTL_SECONDS", "600"))
# How long we wait for the first authenticated API call to fire after
# `page.goto()` returns. 30s leaves plenty of headroom for slow CF challenges
# while still bounding total mint latency.
SESSION_CAPTURE_TIMEOUT_SECONDS = float(os.getenv("KMIANKO_SESSION_CAPTURE_TIMEOUT", "30"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Safari/605.1.15"
)


class KmiankoSessionError(RuntimeError):
    pass


class KmiankoSession:
    """Async-safe singleton session-token holder.

    Concurrent callers awaiting `get_token()` during a refresh share the same
    in-flight Playwright run via the lock — no thundering herd of browsers.

    Also captures the Cloudflare bot-management cookies (notably `__cf_bm`)
    set during the page bootstrap. The bet-slip API host is fronted by CF
    Turnstile/managed-challenge — calls without these cookies are 403'd at
    the edge regardless of the session JWT. Callers MUST send both the
    `session` header AND the cookies returned by `get_cookies()`.
    """

    def __init__(
        self,
        *,
        bootstrap_url: str = DEFAULT_BOOTSTRAP_URL,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        headless: bool = True,
    ):
        self._bootstrap_url = bootstrap_url
        self._ttl = ttl_seconds
        self._headless = headless
        self._token: Optional[str] = None
        self._cookies: dict[str, str] = {}
        self._issued_at: float = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Force the next `get_token()` to mint a fresh one (e.g. after 403)."""
        self._token = None
        self._cookies = {}
        self._issued_at = 0.0

    def _is_fresh(self) -> bool:
        return self._token is not None and (time.time() - self._issued_at) < self._ttl

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.invalidate()
        if self._is_fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            # Re-check inside the lock — another caller may have minted while
            # we were queued.
            if self._is_fresh():
                return self._token  # type: ignore[return-value]
            await self._mint()
            self._issued_at = time.time()
            assert self._token is not None
            logger.info(
                "[KmiankoSession] minted token (len=%d, cookies=%d, ttl=%ds)",
                len(self._token), len(self._cookies), self._ttl,
            )
            return self._token

    async def get_cookies(self) -> dict[str, str]:
        """Return the cookies captured at mint time (e.g. Cloudflare `__cf_bm`)."""
        # Trigger a mint if needed, then return cached cookies.
        await self.get_token()
        return dict(self._cookies)

    async def _mint(self) -> None:
        # Import locally so the rest of the app (and tests that mock this
        # service) don't pay the playwright import cost.
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise KmiankoSessionError(
                "playwright not installed — `pip install playwright && playwright install chromium`"
            ) from exc

        # Telemetry so Railway logs clearly tell us which step failed. The
        # three likely failure modes are: (1) Chromium binary missing (we fail
        # at launch), (2) page loads but Cloudflare shows a challenge page
        # (we see a title like "Just a moment..." and the kmianko API never
        # fires), (3) everything works. Without these logs we can't tell (1)
        # from (2).
        captured: dict[str, str] = {}
        t_start = time.monotonic()

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=self._headless)
            except Exception as exc:
                raise KmiankoSessionError(
                    f"chromium launch failed ({exc!s}) — is the browser binary installed "
                    f"on this host? Run `python -m playwright install chromium`."
                ) from exc
            logger.info("[KmiankoSession] chromium launched (t=%.2fs)", time.monotonic() - t_start)

            try:
                ctx = await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="es-PE",
                    viewport={"width": 1440, "height": 900},
                )
                page = await ctx.new_page()

                def on_request(req):
                    if "kmianko.com/api/" not in req.url:
                        return
                    sess = req.headers.get("session")
                    if sess and "v" not in captured:
                        captured["v"] = sess

                page.on("request", on_request)

                # Capture the top-level navigation response so we can tell
                # apart "CF challenge" (status 403 / cf-mitigated header) from
                # "page loaded fine but no API fired".
                nav_status = {"code": None, "cf_mitigated": None}
                def on_response(resp):
                    if resp.url == self._bootstrap_url or resp.request.resource_type == "document":
                        if nav_status["code"] is None:
                            nav_status["code"] = resp.status
                            nav_status["cf_mitigated"] = resp.headers.get("cf-mitigated")
                page.on("response", on_response)

                try:
                    await page.goto(
                        self._bootstrap_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                except Exception as exc:
                    raise KmiankoSessionError(f"page load failed: {exc}") from exc

                deadline = time.time() + SESSION_CAPTURE_TIMEOUT_SECONDS
                while "v" not in captured and time.time() < deadline:
                    await asyncio.sleep(0.25)

                # Lift cookies for the kmianko host so httpx can replay the
                # CF clearance the browser earned. Also grab the page title
                # and HTML snippet as a CF-challenge tell — the challenge
                # page reads "Just a moment..." in all locales.
                cookie_objs = await ctx.cookies(self._bootstrap_url)
                self._cookies = {c["name"]: c["value"] for c in cookie_objs}
                has_cf_clearance = "cf_clearance" in self._cookies
                has_cf_bm = "__cf_bm" in self._cookies
                page_title = ""
                try:
                    page_title = (await page.title())[:80]
                except Exception:
                    pass
            finally:
                await browser.close()

        elapsed = time.monotonic() - t_start
        if "v" not in captured:
            # Log all the diagnostic state before raising so we can see at a
            # glance whether CF challenged us or the page just failed to fire
            # its API calls.
            looks_like_cf_challenge = (
                (nav_status.get("cf_mitigated") == "challenge")
                or (nav_status.get("code") == 403)
                or ("just a moment" in page_title.lower())
            )
            logger.warning(
                "[KmiankoSession] mint FAILED after %.1fs — nav_status=%s cf_mitigated=%s "
                "cf_bm=%s cf_clearance=%s page_title=%r cookies=%d cf_challenge_suspected=%s",
                elapsed, nav_status.get("code"), nav_status.get("cf_mitigated"),
                has_cf_bm, has_cf_clearance, page_title, len(self._cookies),
                looks_like_cf_challenge,
            )
            if looks_like_cf_challenge:
                raise KmiankoSessionError(
                    f"Cloudflare challenged the bootstrap page (status={nav_status.get('code')}, "
                    f"cf-mitigated={nav_status.get('cf_mitigated')}). Headless Chromium on this "
                    f"egress IP can't pass the challenge. Options: residential proxy, "
                    f"or mint tokens off-box and inject via env."
                )
            raise KmiankoSessionError(
                f"session token never appeared within {SESSION_CAPTURE_TIMEOUT_SECONDS:.0f}s "
                f"(nav_status={nav_status.get('code')}, page_title={page_title!r}, "
                f"cookies={len(self._cookies)}) — browser loaded the page but no kmianko API "
                f"fired. Bootstrap URL may have changed."
            )
        self._token = captured["v"]
        logger.info(
            "[KmiankoSession] mint OK in %.1fs (token_len=%d, cookies=%d, cf_bm=%s, "
            "cf_clearance=%s, nav_status=%s)",
            elapsed, len(self._token), len(self._cookies), has_cf_bm, has_cf_clearance,
            nav_status.get("code"),
        )
