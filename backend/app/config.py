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
    # Defaults shifted 2026-04-24 from 40/30/30 to 30/40/30 per user
    # feedback — the feed was single-heavy after the volume-up PR. BB
    # supply doubled (TACTICAL / MANAGER_QUOTE / PREVIEW went to "both"
    # and BB themes got enriched to 3-4 legs), so there's real BB stock
    # to promote; the ranker now has authority to prefer BB over single
    # when both are queued for the same news.
    out = {"singles": 30, "bb": 40, "combos": 30}
    for part in (s or "").split(","):
        if "=" not in part: continue
        k, v = part.split("=", 1)
        try: out[k.strip().lower()] = max(0, int(v.strip()))
        except ValueError: pass
    return out

PULSE_BET_TYPE_MIX = _parse_mix(os.getenv("PULSE_BET_TYPE_MIX", "singles=30,bb=40,combos=30"))

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

# Five new storyline types (PR: storyline-expansion-top5). All default
# true; flip to "false" on Railway to disable any one detector without a
# redeploy. See `app/engine/storyline_detector.py` for per-type prompts
# and verification logic.
#
#   TITLE_RACE     — top 2-4 clubs within 6 league-points, all in action.
#                    Leg per club: to win (1X2).
#   DERBY_WEEKEND  — >=3 known local / classic rivalry fixtures same week.
#                    Leg per fixture: BTTS Yes (fallback Over 2.5).
#   EUROPEAN_WEEK  — UCL + UEL + UECL midweek. Leg per club: to win (1X2).
#   HOME_FORTRESS  — 4-6 clubs with elite home form all hosting this week.
#                    Leg per club: home win (1X2 home side).
#   GOAL_MACHINES  — 4-6 of Europe's top ~15 scorers all playing; legs are
#                    anytime-scorer per player (cross-league OK, unlike
#                    GOLDEN_BOOT which stays single-league by design).
PULSE_STORYLINE_TITLE_RACE_ENABLED = os.getenv(
    "PULSE_STORYLINE_TITLE_RACE_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_DERBY_WEEKEND_ENABLED = os.getenv(
    "PULSE_STORYLINE_DERBY_WEEKEND_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_EUROPEAN_WEEK_ENABLED = os.getenv(
    "PULSE_STORYLINE_EUROPEAN_WEEK_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_HOME_FORTRESS_ENABLED = os.getenv(
    "PULSE_STORYLINE_HOME_FORTRESS_ENABLED", "true",
).lower() == "true"
PULSE_STORYLINE_GOAL_MACHINES_ENABLED = os.getenv(
    "PULSE_STORYLINE_GOAL_MACHINES_ENABLED", "true",
).lower() == "true"

# Per-type participant caps. GOAL_MACHINES, HOME_FORTRESS, EUROPEAN_WEEK,
# and DERBY_WEEKEND all support up to 6 legs — the narrative scales
# naturally with more actors on the stage. TITLE_RACE caps at 4 (only 4
# clubs ever sit within 6 points of the leader in a real season). Lower
# the caps here if we find 6-leg combos price out too absurd.
PULSE_STORYLINE_GOAL_MACHINES_MAX_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_GOAL_MACHINES_MAX_PARTICIPANTS", "6")
)
PULSE_STORYLINE_HOME_FORTRESS_MAX_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_HOME_FORTRESS_MAX_PARTICIPANTS", "6")
)
PULSE_STORYLINE_EUROPEAN_WEEK_MAX_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_EUROPEAN_WEEK_MAX_PARTICIPANTS", "6")
)
PULSE_STORYLINE_DERBY_WEEKEND_MAX_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_DERBY_WEEKEND_MAX_PARTICIPANTS", "6")
)
PULSE_STORYLINE_TITLE_RACE_MAX_PARTICIPANTS = int(
    os.getenv("PULSE_STORYLINE_TITLE_RACE_MAX_PARTICIPANTS", "4")
)

# ── Storyline standings verification ───────────────────────────────────
# Before a team is emitted as a RELEGATION / EUROPE_CHASE participant,
# the detector hits web_search via a second Haiku call to confirm the
# team's actual league_position (+ points_from_safety / _european_spot
# / form_last_5). Participants that don't meet the positional threshold
# are dropped. If fewer than 2 valid participants survive, the whole
# storyline is skipped — better to ship 0 relegation cards than 1 with a
# mid-table team miscast as fighting the drop.
#
# Kill switch: set PULSE_STORYLINE_STANDINGS_VERIFY_ENABLED=false to
# revert to pre-verification behaviour without a redeploy. Only flip if
# the verification step is mis-identifying participants en masse — the
# whole PR exists to avoid "fellow-relegated" type credibility hits.
PULSE_STORYLINE_STANDINGS_VERIFY_ENABLED = os.getenv(
    "PULSE_STORYLINE_STANDINGS_VERIFY_ENABLED", "true",
).lower() == "true"

# RELEGATION threshold: team qualifies if league_position >=
# (league_size - PULSE_STORYLINE_RELEGATION_MAX_POSITION) (i.e. bottom-N
# of its league — default 6) OR points_from_safety <=
# PULSE_STORYLINE_RELEGATION_MAX_POINTS_FROM_SAFETY (default 6).
PULSE_STORYLINE_RELEGATION_MAX_POSITION = int(
    os.getenv("PULSE_STORYLINE_RELEGATION_MAX_POSITION", "6")
)
PULSE_STORYLINE_RELEGATION_MAX_POINTS_FROM_SAFETY = int(
    os.getenv("PULSE_STORYLINE_RELEGATION_MAX_POINTS_FROM_SAFETY", "6")
)

# EUROPE_CHASE threshold: team qualifies if league_position in
# [MIN_POSITION, MAX_POSITION] (default 3..7) OR
# points_from_european_spot <= MAX_POINTS (default 5).
PULSE_STORYLINE_EUROPE_CHASE_MIN_POSITION = int(
    os.getenv("PULSE_STORYLINE_EUROPE_CHASE_MIN_POSITION", "3")
)
PULSE_STORYLINE_EUROPE_CHASE_MAX_POSITION = int(
    os.getenv("PULSE_STORYLINE_EUROPE_CHASE_MAX_POSITION", "7")
)
PULSE_STORYLINE_EUROPE_CHASE_MAX_POINTS_FROM_SPOT = int(
    os.getenv("PULSE_STORYLINE_EUROPE_CHASE_MAX_POINTS_FROM_SPOT", "5")
)

# Model used by the standings-verification Haiku call. Haiku 4.5 is the
# sweet spot — cheap, has web_search, doesn't over-think the JSON shape.
PULSE_STORYLINE_VERIFY_MODEL = os.getenv(
    "PULSE_STORYLINE_VERIFY_MODEL", "claude-haiku-4-5",
)

# Borderline-participant tolerance (2026-04-24 mix-balance PR). When
# the standings-verify pass drops enough participants that only 2
# survive, check any dropped candidate whose row is within 1 position
# OR within 2 points of the threshold (relegation points_from_safety /
# europe_chase points_from_european_spot) and re-include it as a
# borderline participant. Gets storyline combos from 2 legs to 3 legs
# more often while keeping the hard-verified gate intact for first-pass
# cuts. Kill-switch: set false to revert to strict verification.
PULSE_STORYLINE_BORDERLINE_TOLERANCE_ENABLED = os.getenv(
    "PULSE_STORYLINE_BORDERLINE_TOLERANCE_ENABLED", "true",
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
# Volume-up defaults (2026-04-24, PR #53): HOT cadence halved to 30 min,
# HOT/WARM fixture caps raised to 10 so the social feed sustains the
# locked 20-30 cards/hour target. Boot-freshness skip (below) keeps cost
# in check by cache-hitting fixtures whose news is still fresh.
PULSE_TIER_HOT_MIN_SECONDS = int(os.getenv("PULSE_TIER_HOT_MIN_SECONDS", "1800"))
PULSE_TIER_WARM_MIN_SECONDS = int(os.getenv("PULSE_TIER_WARM_MIN_SECONDS", "3600"))
PULSE_TIER_COOL_MIN_SECONDS = int(os.getenv("PULSE_TIER_COOL_MIN_SECONDS", "21600"))
PULSE_TIER_COLD_MIN_SECONDS = int(os.getenv("PULSE_TIER_COLD_MIN_SECONDS", "43200"))
PULSE_TIER_HOT_MAX_FIXTURES = int(os.getenv("PULSE_TIER_HOT_MAX_FIXTURES", "5"))
PULSE_TIER_WARM_MAX_FIXTURES = int(os.getenv("PULSE_TIER_WARM_MAX_FIXTURES", "10"))
PULSE_TIER_COOL_MAX_FIXTURES = int(os.getenv("PULSE_TIER_COOL_MAX_FIXTURES", "6"))
PULSE_TIER_COLD_MAX_FIXTURES = int(os.getenv("PULSE_TIER_COLD_MAX_FIXTURES", "4"))

# ── HOT-tier classifier (smart fixture filter) ─────────────────────────
# The HOT tier (kickoff <6h) used to scoop the first N fixtures by
# kickoff time. The classifier narrows this to: (a) fixtures with
# kickoff in a usable window (skip already-started, skip far-out),
# (b) top-league only — see catalogue_loader.INTERNATIONAL_LEAGUE_PATTERNS,
# (c) BetBuilder-eligible only (fail-soft if the catalogue row doesn't
# carry the flag), (d) cap at PULSE_TIER_HOT_MAX_FIXTURES sorted by
# soonest kickoff first. Filters are cheap (no LLM), each kill-switched.
PULSE_HOT_MIN_KICKOFF_MINUTES = int(
    os.getenv("PULSE_HOT_MIN_KICKOFF_MINUTES", "90")
)
PULSE_HOT_MAX_KICKOFF_HOURS = int(
    os.getenv("PULSE_HOT_MAX_KICKOFF_HOURS", "6")
)
PULSE_HOT_REQUIRE_BB_ENABLED = os.getenv(
    "PULSE_HOT_REQUIRE_BB_ENABLED", "true",
).lower() == "true"
PULSE_HOT_TOP_LEAGUE_ONLY = os.getenv(
    "PULSE_HOT_TOP_LEAGUE_ONLY", "true",
).lower() == "true"

# Per-fixture candidate cap (applies inside PolicyLayer). Old implicit
# default was 3; raising to 5 so high-conviction fixtures (Premier League
# with multiple storylines) can surface more supply to the ranker.
PULSE_NEWS_CANDIDATES_PER_FIXTURE_MAX = int(
    os.getenv("PULSE_NEWS_CANDIDATES_PER_FIXTURE_MAX", "5")
)

# Boot-freshness skip: before scouting a fixture in a tier cycle, check
# the DB for the latest `news_items.ingested_at` for that fixture. If it
# is newer than the tier's own cadence, skip the scout entirely — news
# is fresh, candidates + prices will rebuild from cache. This is the
# money-saver: on a redeploy the DB already holds fresh news from the
# most recent cycle, so we don't re-pay Haiku + web_search on boot.
#
# Kill-switch: set false to force every tier tick to scout regardless of
# DB freshness (demo mode / forced-content scenario).
PULSE_BOOT_FRESHNESS_SKIP_ENABLED = os.getenv(
    "PULSE_BOOT_FRESHNESS_SKIP_ENABLED", "true",
).lower() == "true"

# Freshness slack window. The tier-loop freshness check compares
# (now - latest_ingested_at) against (tier_cadence + slack). Without
# slack, a fixture scouted T seconds ago when cadence is also T seconds
# never qualifies as "fresh" (the comparison is on the boundary), so
# `skipped_fresh` stays at 0 even when the cache is hot. Default 300s
# (5min) covers boundary jitter + scout duration.
PULSE_TIER_FRESHNESS_SLACK_SECONDS = int(
    os.getenv("PULSE_TIER_FRESHNESS_SLACK_SECONDS", "300")
)

# ── Storyline cooldowns ────────────────────────────────────────────────
# Per (storyline_type, league) cooldown to avoid re-scouting standings
# every tier cycle. Standings change at most once a day; scouting them
# every 30-60 minutes burns Haiku+web_search calls for zero new info.
# Default 6h. Per-type override:
#   PULSE_STORYLINE_<TYPE>_COOLDOWN_SECONDS   (e.g. _GOLDEN_BOOT_=14400)
PULSE_STORYLINE_COOLDOWN_SECONDS = int(
    os.getenv("PULSE_STORYLINE_COOLDOWN_SECONDS", "21600")
)

# Standings-verify cache TTL. The (team, today) keyed cache in
# storyline_detector held a hard-coded 12h TTL — env-knobbed here so
# we can tune without a redeploy. 12h default unchanged.
PULSE_STANDINGS_CACHE_TTL_SECONDS = int(
    os.getenv("PULSE_STANDINGS_CACHE_TTL_SECONDS", "43200")
)

# ── Cycle cost telemetry ───────────────────────────────────────────────
# Per-MTOKEN coefficients (HAIKU_INPUT/OUTPUT/CACHE_*) live in the
# "Anthropic Haiku 4.5 published rates" block below. The legacy
# per-call estimates (PULSE_COST_HAIKU_PER_CALL / PULSE_COST_SONNET_PER_CALL
# / PULSE_COST_HAIKU_WEBSEARCH_PER_CALL) were removed 2026-04-27 — they
# leaked Sonnet rates into the cycle-cost log after the Haiku 4.5
# migration in PR #62 (e.g. `$0.275 / 5 calls = $0.055/call`). All cost
# logging now reads actual recorded spend from `cost_tracker`, which
# in turn computes cost from real `response.usage` token counts.

# ── Daily LLM-spend tripwire (cost-aware redesign, 2026-04-26) ─────────
# Hard kill at threshold. Engine self-pauses when today_total + projected
# would exceed budget × 0.99. Knobs are env-only so ops can flip on
# Railway without a redeploy.
#
# The granular per-million-token rates live alongside the daily budget
# so a CostTracker built without explicit overrides reads them from os
# env at import time (see app/services/cost_tracker.py).
PULSE_DAILY_LLM_BUDGET_USD = float(
    os.getenv("PULSE_DAILY_LLM_BUDGET_USD", "3")
)
PULSE_DAILY_WEBSEARCH_BUDGET = int(
    os.getenv("PULSE_DAILY_WEBSEARCH_BUDGET", "100")
)

# Anthropic Haiku 4.5 published rates (as of 2026-04). Per million
# tokens. Cache writes split by TTL: 5m default, 1h variant for the
# storyline narrative path that fires less than once per hour.
PULSE_COST_HAIKU_INPUT_PER_MTOKEN_USD = float(
    os.getenv("PULSE_COST_HAIKU_INPUT_PER_MTOKEN_USD", "1.0")
)
PULSE_COST_HAIKU_OUTPUT_PER_MTOKEN_USD = float(
    os.getenv("PULSE_COST_HAIKU_OUTPUT_PER_MTOKEN_USD", "5.0")
)
PULSE_COST_HAIKU_CACHE_READ_PER_MTOKEN_USD = float(
    os.getenv("PULSE_COST_HAIKU_CACHE_READ_PER_MTOKEN_USD", "0.10")
)
PULSE_COST_HAIKU_CACHE_WRITE_PER_MTOKEN_USD = float(
    os.getenv("PULSE_COST_HAIKU_CACHE_WRITE_PER_MTOKEN_USD", "1.25")
)
PULSE_COST_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD = float(
    os.getenv("PULSE_COST_HAIKU_CACHE_WRITE_1H_PER_MTOKEN_USD", "2.0")
)
PULSE_COST_WEBSEARCH_PER_CALL_USD = float(
    os.getenv("PULSE_COST_WEBSEARCH_PER_CALL_USD", "0.025")
)

# ── Out-of-band alerts ────────────────────────────────────────────────
# Where the in-process alert emitter (app/services/alert_emitter.py)
# POSTs critical events. Today the only consumer is the cost-tripwire
# (engine self-paused because daily LLM budget exhausted). When unset
# the emitter logs at WARNING — Sentry's FastApiIntegration captures
# WARNINGs as breadcrumbs — but doesn't try to POST anywhere.
#
# Accepts any HTTPS endpoint that consumes a JSON body shaped like:
#   {"level":"critical","title":"...","body":"...",
#    "timestamp":"<ISO-8601 UTC>","project":"pulse"}
# i.e. Slack incoming webhook, Telegram-bot relay, custom Pipedream URL.
# Empty string is the kill switch.
PULSE_ALERTS_WEBHOOK_URL = os.getenv("PULSE_ALERTS_WEBHOOK_URL", "")

# Hook-diversity release buffer (social-feed stream ordering rule).
# Candidates that pass gates are buffered for up to this many seconds
# instead of broadcasting immediately. A single release scheduler wakes
# ~every 20s and picks the next-best buffered card whose hook_type
# differs from the last 2 published. If no match found, the oldest card
# in the buffer is released anyway (no starvation).
#
# Set to 0 to disable buffering (cards broadcast immediately, pre-PR #53
# behaviour).
PULSE_PUBLISH_BUFFER_SECONDS = int(
    os.getenv("PULSE_PUBLISH_BUFFER_SECONDS", "60")
)

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

# ── Embed token + domain allowlist (PR feat/embed-token-contract) ──────
# Per-operator widget gate. The /api/feed (and /api/feed/*) routes accept
# an `embed_token` query param OR an X-Pulse-Embed-Token header; the
# backend looks the token up in the `embeds` table and verifies the
# request's Origin (or Referer fallback) host matches the embed's
# `allowed_origins` list. /admin/embeds is the inspector + form-based
# CRUD surface.
#
# Kill switch defaults to OFF (false) so the live widget keeps working
# through the cutover. Flip to "true" on Railway after Apuesta Total has
# the seeded token wired into their iframe; no redeploy needed.
PULSE_EMBED_TOKEN_REQUIRED = os.getenv(
    "PULSE_EMBED_TOKEN_REQUIRED", "false",
).lower() == "true"

# Reserved for a future TTL-based token expiry. 0 = never expire (current
# behaviour). Knob is wired so wave-2 can introduce expiry without a
# config-shape change.
PULSE_EMBED_DEFAULT_TTL_SECONDS = int(
    os.getenv("PULSE_EMBED_DEFAULT_TTL_SECONDS", "0")
)
