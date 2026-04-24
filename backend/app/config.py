import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_LLM = os.getenv("USE_LLM", "false").lower() == "true"
SIMULATOR_SPEED = float(os.getenv("SIMULATOR_SPEED", "20"))  # seconds between major game events
SIMULATOR_EVENT_DELAY = float(os.getenv("SIMULATOR_EVENT_DELAY", "6"))  # seconds between cards within a single game tick

# ── Data source ──
# "mock" (default) uses the pre-scripted LAL/ARS/KC timelines against local JSON fixtures.
# "rogue" pulls real pre-match soccer fixtures from the Rogue API on startup.
PULSE_DATA_SOURCE = os.getenv("PULSE_DATA_SOURCE", "mock").lower()

# ── Rogue API ──
ROGUE_BASE_URL = os.getenv(
    "ROGUE_BASE_URL",
    "https://prod20392-168426033.msjxk.com/api/rogue",
)
ROGUE_CONFIG_JWT = os.getenv("ROGUE_CONFIG_JWT", "")
ROGUE_RATE_LIMIT_PER_SECOND = float(os.getenv("ROGUE_RATE_LIMIT_PER_SECOND", "5"))

# Catalogue loader scope
ROGUE_CATALOGUE_DAYS_AHEAD = int(os.getenv("ROGUE_CATALOGUE_DAYS_AHEAD", "7"))
ROGUE_CATALOGUE_MAX_EVENTS = int(os.getenv("ROGUE_CATALOGUE_MAX_EVENTS", "25"))

# Soccer sport ID in Rogue. 1 is Soccer per the MCP docs.
ROGUE_SOCCER_SPORT_ID = os.getenv("ROGUE_SOCCER_SPORT_ID", "1")

# ── Recommendation engine ──
# News ingestion + candidate store. In production we mount a Railway volume
# at /data and set PULSE_DB_PATH=/data/pulse.db so the cache survives
# redeploys (U1). Locally we default to ./pulse.db in the CWD — kept out
# of git via .gitignore.
PULSE_DB_PATH = os.getenv("PULSE_DB_PATH", "./pulse.db")

# Per-fixture news scouting budget. Each run calls Haiku 4.5 + web_search.
PULSE_NEWS_MODEL = os.getenv("PULSE_NEWS_MODEL", "claude-haiku-4-5")
PULSE_NEWS_MAX_SEARCHES = int(os.getenv("PULSE_NEWS_MAX_SEARCHES", "5"))
PULSE_NEWS_MAX_FIXTURES = int(os.getenv("PULSE_NEWS_MAX_FIXTURES", "12"))
PULSE_NEWS_CACHE_TTL_HOURS = int(os.getenv("PULSE_NEWS_CACHE_TTL_HOURS", "6"))

# Cost controls.
#   PULSE_NEWS_INGEST_ENABLED=false  → skip ALL scout calls (kill switch).
#                                       Featured BBs still load (no LLM).
#                                       Useful when iterating without a demo,
#                                       or when the API key is out of credits.
PULSE_NEWS_INGEST_ENABLED = os.getenv("PULSE_NEWS_INGEST_ENABLED", "true").lower() == "true"

# Publish gate. Only candidates at or above this score reach the public feed.
PULSE_PUBLISH_THRESHOLD = float(os.getenv("PULSE_PUBLISH_THRESHOLD", "0.55"))

# ── Bet-type mix ────────────────────────────────────────────────────────
# Target distribution for bet types on the published feed. Format:
# `singles=N,bb=N,combos=N` where N values are integer weights (need not
# sum to 100). Defaults to balanced 40/30/30.
#
# Phase 1: parsed but not yet enforced — the engine emits whatever it emits.
# Phase 2 (next PR): publisher caps emitted candidates per bet_type to honor
# the target weights, with overflow carried into next-cycle priority.
# Phase 3 (later): admin UI sliders + click-tracking learning loop adjusts
# weights nightly based on which bet_types get CTA-clicked through.
def _parse_mix(s: str) -> dict[str, int]:
    out = {"singles": 40, "bb": 30, "combos": 30}
    for part in (s or "").split(","):
        if "=" not in part: continue
        k, v = part.split("=", 1)
        try: out[k.strip().lower()] = max(0, int(v.strip()))
        except ValueError: pass
    return out

PULSE_BET_TYPE_MIX = _parse_mix(os.getenv("PULSE_BET_TYPE_MIX", "singles=40,bb=30,combos=30"))

# Hook-variety guard (post-mix-quota rank pass). When true, the feed ranker
# walks the ordered list and, if two consecutive cards share hook_type
# regardless of league, tries to swap the second with a later card whose
# hook_type differs from both neighbours. Budget-capped at 5 swaps per
# rank pass. Default on; flip to "false" to revert to the prior league+
# hook demotion only, without a redeploy.
PULSE_HOOK_VARIETY_GUARD_ENABLED = os.getenv(
    "PULSE_HOOK_VARIETY_GUARD_ENABLED", "true",
).lower() == "true"

# ── Per-hook BB/single preference (supply policy) ──────────────────────
# Optional JSON override of the hardcoded `HOOK_BET_TYPE_PREFERENCE` dict
# in `engine/news_scorer.py`. Keys are HookType enum values (strings like
# "injury", "tactical"); values are "bb", "single", or "both". Anything
# not overridden falls back to the module default. Missing / malformed
# JSON falls back silently so a bad env flag can't kill the engine.
#
# Example:
#   PULSE_HOOK_BET_TYPE_PREFERENCE_JSON='{"tactical":"bb","preview":"both"}'
#
# Lets ops A/B per-hook shape without a redeploy.
import json as _json
def _parse_hook_pref(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = _json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        v_norm = v.strip().lower()
        if v_norm not in {"bb", "single", "both"}:
            continue
        out[k.strip().lower()] = v_norm
    return out

PULSE_HOOK_BET_TYPE_PREFERENCE_JSON = _parse_hook_pref(
    os.getenv("PULSE_HOOK_BET_TYPE_PREFERENCE_JSON", "")
)

# Minimum participants required for a cross-event storyline (Golden Boot
# race, relegation battle, etc.) to be considered viable. 2 is the
# supply-friendly floor — a weekend with only two top-scorer contenders
# in action is still a legitimate combo story. Raise to 3 if we find
# 2-participant stories dilute quality.
PULSE_STORYLINE_MIN_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_MIN_PARTICIPANTS", "2")
)

# Per-storyline-type kill switches. All default true so a deploy picks up
# the new types automatically; set any to "false" to disable the
# corresponding detector without a redeploy (Railway env flip). The
# top-level `PULSE_STORYLINE_COMBOS_ENABLED` master switch gates the
# whole storyline step and still applies.
PULSE_STORYLINE_GOLDEN_BOOT_ENABLED = os.getenv(
    "PULSE_STORYLINE_GOLDEN_BOOT_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_RELEGATION_ENABLED = os.getenv(
    "PULSE_STORYLINE_RELEGATION_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_EUROPE_CHASE_ENABLED = os.getenv(
    "PULSE_STORYLINE_EUROPE_CHASE_ENABLED", "true",
).lower() == "true"

# ── Stage 5 — deep-link CTA ────────────────────────────────────────────
# The card's "Tap to bet" / "Add Bet Builder" CTA opens this URL (target=
# _blank) with the card's Rogue selection_ids pre-loaded on the operator's
# bet slip. Discovered Phase 1 for Apuesta Total: the sportsbook runs
# inside a kmianko iframe (`/es-pe/spbkv3`) whose parent page at
# `apuestatotal.com/apuestas-deportivas` forwards the query string
# *inside* the `fpath` param to the iframe. The iframe reads `selectionId`
# from its own URL and fires the addSelection flow on boot (see
# km_index.js → queryOverrides.bscode / redirectData.selectionId).
#
# Three templates so we can evolve per-bet-type independently without
# redeploying. `{selection_ids}` is comma-joined Rogue IDs (legs for
# combos, single id for singles); `{virtual_selection}` is the BB's
# `0VS<leg1>|<leg2>|…` virtual-selection id (same shape the kmianko
# iframe already recognises as a BetBuilderBet thanks to the Vd="0VS"
# prefix check and `si()` pipe-split helper).
#
# For cross-event combos we deep-link to the FIRST leg only — the kmianko
# iframe currently picks up one `selectionId` per load and ignores a
# repeated param. That's a known operator limitation; the frontend still
# renders the full leg list, the user just has to add legs 2+ manually
# after the first lands. When the operator ships `bscode` minting we can
# swap this for a multi-leg template.
PULSE_DEEPLINK_ENABLED = os.getenv("PULSE_DEEPLINK_ENABLED", "true").lower() == "true"
PULSE_DEEPLINK_TEMPLATE_SINGLE = os.getenv(
    "PULSE_DEEPLINK_TEMPLATE_SINGLE",
    # Default: Apuesta Total pre-match sportsbook; iframe path encoded
    # inside `fpath` so the iframe's own query string carries selectionId.
    "https://www.apuestatotal.com/apuestas-deportivas?fpath=%2Fes-pe%2Fspbkv3%3FselectionId%3D{selection_ids}",
)
PULSE_DEEPLINK_TEMPLATE_BB = os.getenv(
    "PULSE_DEEPLINK_TEMPLATE_BB",
    # BB: pass the virtual-selection id. kmianko treats 0VS-prefixed IDs
    # as BetBuilder and splits on `|` internally.
    "https://www.apuestatotal.com/apuestas-deportivas?fpath=%2Fes-pe%2Fspbkv3%3FselectionId%3D{virtual_selection}",
)
PULSE_DEEPLINK_TEMPLATE_COMBO = os.getenv(
    "PULSE_DEEPLINK_TEMPLATE_COMBO",
    # Cross-event combo / storyline: first leg only (see caveat above).
    "https://www.apuestatotal.com/apuestas-deportivas?fpath=%2Fes-pe%2Fspbkv3%3FselectionId%3D{selection_ids}",
)

# ── Stage 5b — server-minted bscode deep links ─────────────────────────
# PR #36's `?selectionId=...` URL only restores one leg; the operator's
# actual mechanism is a 6-char `bscode` minted server-side via kmianko's
# share-betslip endpoint, which restores the full BB / combo / single
# slip verbatim. We fetch anonymous JWTs from the spbkv3 HTML (no login),
# POST the selection list to /api/betslip/betslip/share-betslip, and
# swap the deep-link URL shape to carry the returned code inside fpath.
#
# Kill-switch: set PULSE_KMIANKO_BSCODE_ENABLED=false to revert to the
# PR #36 selectionId URLs without a redeploy. Mint failures always fall
# back to the selectionId URL too.
PULSE_KMIANKO_BSCODE_ENABLED = os.getenv(
    "PULSE_KMIANKO_BSCODE_ENABLED", "true",
).lower() == "true"
PULSE_KMIANKO_BASE_URL = os.getenv(
    "PULSE_KMIANKO_BASE_URL", "https://prod20392.kmianko.com",
)
PULSE_KMIANKO_SPBKV3_PATH = os.getenv(
    "PULSE_KMIANKO_SPBKV3_PATH", "/es-pe/spbkv3",
)
# The outer `apuestatotal.com` wrapper — same base for all bet types.
# `{bscode}` is URL-substituted into the fpath-encoded iframe path.
PULSE_OPERATOR_WRAPPER_URL = os.getenv(
    "PULSE_OPERATOR_WRAPPER_URL",
    "https://www.apuestatotal.com/apuestas-deportivas",
)
# Final shape:
#   {wrapper}?fpath=%2Fes-pe%2Fspbkv3%3Fbscode%3D{bscode}
# Single template — bscode is bet-type-agnostic because kmianko restores
# the full slip (single, BB, combo) from the code alone.
PULSE_DEEPLINK_TEMPLATE_BSCODE = os.getenv(
    "PULSE_DEEPLINK_TEMPLATE_BSCODE",
    # Default composes wrapper + iframe path + bscode query param.
    "{wrapper}?fpath=%2Fes-pe%2Fspbkv3%3Fbscode%3D{bscode}",
)

# ── Tiered freshness / staggered publish ───────────────────────────────
# Social-feed cadence: instead of one 4h atomic cycle, run tier-specific
# loops that re-scout fixtures by kickoff proximity. Cards stream in one
# at a time (see PULSE_STAGGERED_PUBLISH_ENABLED below) so the feed feels
# alive across the hour.
#
# Tiers: HOT (<6h to kickoff, every 60 min) / WARM (6-24h, every 2h) /
# COOL (24-72h, every 6h) / COLD (>72h, every 12h).
#
# Kill switch: set PULSE_TIERED_FRESHNESS_ENABLED=false to revert to the
# single-cycle PULSE_RERUN_INTERVAL_SECONDS path.
PULSE_TIERED_FRESHNESS_ENABLED = os.getenv(
    "PULSE_TIERED_FRESHNESS_ENABLED", "true",
).lower() == "true"
PULSE_TIER_HOT_MIN_SECONDS = int(os.getenv("PULSE_TIER_HOT_MIN_SECONDS", "3600"))
PULSE_TIER_WARM_MIN_SECONDS = int(os.getenv("PULSE_TIER_WARM_MIN_SECONDS", "7200"))
PULSE_TIER_COOL_MIN_SECONDS = int(os.getenv("PULSE_TIER_COOL_MIN_SECONDS", "21600"))
PULSE_TIER_COLD_MIN_SECONDS = int(os.getenv("PULSE_TIER_COLD_MIN_SECONDS", "43200"))
PULSE_TIER_HOT_MAX_FIXTURES = int(os.getenv("PULSE_TIER_HOT_MAX_FIXTURES", "5"))
PULSE_TIER_WARM_MAX_FIXTURES = int(os.getenv("PULSE_TIER_WARM_MAX_FIXTURES", "8"))
PULSE_TIER_COOL_MAX_FIXTURES = int(os.getenv("PULSE_TIER_COOL_MAX_FIXTURES", "6"))
PULSE_TIER_COLD_MAX_FIXTURES = int(os.getenv("PULSE_TIER_COLD_MAX_FIXTURES", "4"))

# Staggered publish: each candidate is broadcast as it passes gates, not in
# an atomic batch swap. Disable with false to revert to batch behaviour
# (useful if the per-card broadcast firehose is overwhelming clients).
PULSE_STAGGERED_PUBLISH_ENABLED = os.getenv(
    "PULSE_STAGGERED_PUBLISH_ENABLED", "true",
).lower() == "true"

# Card TTL — each published card falls off the feed (and is broadcast as
# removed) after this many seconds. Default 6h matches the news cache TTL
# so we don't show cards that pre-date the scout's cached info.
PULSE_CARD_TTL_SECONDS = int(os.getenv("PULSE_CARD_TTL_SECONDS", "21600"))

# How often the TTL sweep runs (checking for expired cards).
PULSE_CARD_TTL_SWEEP_SECONDS = int(os.getenv("PULSE_CARD_TTL_SWEEP_SECONDS", "60"))

# Default TRUE: emit the direct kmianko URL instead of the apuestatotal.com
# wrapper. Discovered post-PR #37 that the outer wrapper is a Next.js SPA
# that builds the kmianko iframe client-side — its fpath decoder strips or
# mangles the `bscode` query param when composing the iframe URL, so the
# slip never hydrates. The direct kmianko URL ships `APP_QUERY_OVERRIDES =
# {'bscode': '...'}` in the SSR HTML, which the bundle's useEffect reads to
# fire `sendBsEvent({action:'GET_SHARED_BETSLIP', payload:{code:t}})` on
# boot. Flip to false to restore the wrapper URL (PR #37 shape) without a
# redeploy if needed.
PULSE_DEEPLINK_USE_DIRECT_KMIANKO = os.getenv(
    "PULSE_DEEPLINK_USE_DIRECT_KMIANKO", "true",
).lower() == "true"
# Direct-kmianko shape:
#   {kmianko_base}{spbkv3_path}?bscode={bscode}
PULSE_DEEPLINK_TEMPLATE_BSCODE_DIRECT = os.getenv(
    "PULSE_DEEPLINK_TEMPLATE_BSCODE_DIRECT",
    "{kmianko_base}{spbkv3_path}?bscode={bscode}",
)
