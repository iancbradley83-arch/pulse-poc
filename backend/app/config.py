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
# News ingestion + candidate store live under backend/app/data/. Kept out of
# git via .gitignore — it's a local, rebuildable cache plus candidate history.
PULSE_DB_PATH = os.getenv("PULSE_DB_PATH", "app/data/pulse.db")

# Per-fixture news scouting budget. Each run calls Haiku 4.5 + web_search.
PULSE_NEWS_MODEL = os.getenv("PULSE_NEWS_MODEL", "claude-haiku-4-5")
PULSE_NEWS_MAX_SEARCHES = int(os.getenv("PULSE_NEWS_MAX_SEARCHES", "5"))
PULSE_NEWS_MAX_FIXTURES = int(os.getenv("PULSE_NEWS_MAX_FIXTURES", "12"))
PULSE_NEWS_CACHE_TTL_HOURS = int(os.getenv("PULSE_NEWS_CACHE_TTL_HOURS", "6"))

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
