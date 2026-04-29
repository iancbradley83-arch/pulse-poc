"""
Per-command help topics for /help <command> and /help_<command>.

Each entry is scenario-led: when do I reach for this, what does it show,
concrete examples. No emoji. Monospace-aligned.

This module owns all topic text. Do not inline topic strings into
formatting.py or handlers.py.
"""

TOPICS: dict[str, str] = {
    "status": (
        "/status — pulse health at a glance\n"
        "\n"
        "When to use:\n"
        "  - Drop in mid-day and want to know if anything is wrong\n"
        "  - After a redeploy — confirm it landed and the engine came up\n"
        "  - Operator pings about a problem — first stop before digging deeper\n"
        "\n"
        "What it shows:\n"
        "  Pulse health (ok / DOWN + last-green age), today's cost vs budget,\n"
        "  latest deploy (status, commit, age), feed card count, engine\n"
        "  kill-switch states (rerun / news / storylines), $/card KPI.\n"
        "\n"
        "Examples:\n"
        "  /status"
    ),

    "cost": (
        "/cost [days] — daily LLM spend across days\n"
        "\n"
        "When to use:\n"
        "  - Spot a trend: today vs yesterday vs last week\n"
        "  - Budget review at end of week (cap at 30 days)\n"
        "  - Cost looks high in /status — confirm it's today, not a spike carryover\n"
        "\n"
        "What it shows:\n"
        "  One row per day: date, spend, budget, %, call count.\n"
        "  Default last 3 days. Max 30.\n"
        "\n"
        "Examples:\n"
        "  /cost              last 3 days\n"
        "  /cost 7            last 7 days\n"
        "  /cost 30           last 30 days (max)"
    ),

    "breakdown": (
        "/breakdown — today's spend split by kind + unit economics\n"
        "\n"
        "When to use:\n"
        "  - /cost looks high — figure out which bucket is burning\n"
        "  - Check $/card to see if the engine is efficient today\n"
        "  - Verify cache is working (rewrite cache hits should be non-zero)\n"
        "\n"
        "What it shows:\n"
        "  Total spend + %, breakdown by kind (scout / rewrite / verify etc),\n"
        "  cards in feed, unique cards published today, $/card in feed,\n"
        "  $/unique card today, rewrite cache hit count, churn footnote if\n"
        "  republishes are unusually high.\n"
        "\n"
        "Examples:\n"
        "  /breakdown"
    ),

    "feed": (
        "/feed — feed content audit\n"
        "\n"
        "When to use:\n"
        "  - Confirm content variety and price coverage at a glance\n"
        "  - Hook mix looks skewed (e.g. all ANTEPOST, no PLAYER_PROP)\n"
        "  - Operator reports cards are all from one league\n"
        "  - Checking suspended card count before a busy match day\n"
        "\n"
        "What it shows:\n"
        "  Total card count, by-hook-type breakdown, top 5 leagues,\n"
        "  missing-price count, suspended count, avg relevance score.\n"
        "\n"
        "Examples:\n"
        "  /feed\n"
        "  (then /cards to scroll the actual cards)"
    ),

    "cards": (
        "/cards [page] — paginated card list\n"
        "\n"
        "When to use:\n"
        "  - Scroll the live feed to get card IDs for /card\n"
        "  - Sanity-check what users are actually seeing\n"
        "  - Confirm a specific game or hook type is represented\n"
        "\n"
        "What it shows:\n"
        "  5 cards per page: 8-char ID, hook type, odds, game, narrative.\n"
        "  Tap /cards_2 (or /cards_3 etc) in the footer to paginate.\n"
        "\n"
        "Examples:\n"
        "  /cards             page 1\n"
        "  /cards 3           page 3\n"
        "  /cards_2           tappable shortcut for page 2"
    ),

    "card": (
        "/card <id> — full card detail\n"
        "\n"
        "When to use:\n"
        "  - Operator or user reports a specific card is wrong\n"
        "  - Check narrative, selections, and odds for a card you spotted in /cards\n"
        "  - Confirm suspended flag before escalating\n"
        "\n"
        "What it shows:\n"
        "  Game, hook type, narrative, headline, legs + prices, total odds,\n"
        "  relevance score, suspended flag, deep link, published age.\n"
        "  Bare /card lists 5 recent card IDs to copy from.\n"
        "\n"
        "Examples:\n"
        "  /card              list recent card IDs\n"
        "  /card a1b2c3d4     full detail by 8-char prefix\n"
        "  /card a1b2c3d4-e5f6-...  full UUID also works"
    ),

    "embed": (
        "/embed [slug] — operator embed config\n"
        "\n"
        "When to use:\n"
        "  - Operator says widget is broken on their site — confirm token + domain\n"
        "  - Onboarding handover — verify embed is active and configured correctly\n"
        "  - Investigating why a card isn't loading on a specific operator's domain\n"
        "\n"
        "What it shows:\n"
        "  Slug, scrubbed token (first 8 chars + ***), allowed domains,\n"
        "  theme overrides count, created age, last-served age, active flag.\n"
        "  Bare /embed lists all configured slugs.\n"
        "\n"
        "Examples:\n"
        "  /embed                  list configured operator slugs\n"
        "  /embed apuesta-total    full config for that operator"
    ),

    "logs": (
        "/logs [n] — recent WARN/ERROR lines from pulse-poc\n"
        "\n"
        "When to use:\n"
        "  - An alert fired and you want to see what the engine was doing\n"
        "  - After a deploy — confirm nothing is throwing on startup\n"
        "  - Operator reports intermittent failures — look for tracebacks\n"
        "\n"
        "What it shows:\n"
        "  Last n WARN/ERROR/Traceback lines from the latest pulse-poc\n"
        "  deployment on Railway. Default 20, max 100. Timestamps in HH:MM:SS.\n"
        "\n"
        "Examples:\n"
        "  /logs              last 20 warn/error lines\n"
        "  /logs 50           last 50\n"
        "  /logs 100          max"
    ),

    "runbook": (
        "/runbook [topic] — RUNBOOK.md sections by keyword\n"
        "\n"
        "When to use:\n"
        "  - 3am brain fog: forgotten how to roll back, pull logs, tune limits\n"
        "  - Mid-incident: need the exact steps for a known failure mode\n"
        "  - Onboarding: look up setup or config procedures without opening a laptop\n"
        "\n"
        "What it shows:\n"
        "  The section(s) from pulse-poc/docs/RUNBOOK.md whose heading or body\n"
        "  matches your keyword (case-insensitive). Multiple matches → list of\n"
        "  section names to narrow down. Bare /runbook lists all topics.\n"
        "\n"
        "Examples:\n"
        "  /runbook               list all topics\n"
        "  /runbook 502           section matching '502'\n"
        "  /runbook rollback      section matching 'rollback'\n"
        "  /runbook rate limit    multi-word keyword"
    ),

    "playbook": (
        "/playbook [topic] — what to do when X happens\n"
        "\n"
        "When to use:\n"
        "  - A push alert just fired and you want a triage sequence\n"
        "  - Operator reports a problem and you need a checklist\n"
        "  - 3am wake-up: 'is this worth waking up for?' — see the wake-up matrix\n"
        "  - You just hit a new failure mode — read after the fix and add a section\n"
        "\n"
        "What it shows:\n"
        "  Sections from pulse-poc/docs/PLAYBOOK.md matching your keyword.\n"
        "  Each scenario covers symptom, first move, common causes, phone fix,\n"
        "  when to escalate to a laptop, and the learning step (open an incident).\n"
        "  Bare /playbook lists every scenario.\n"
        "\n"
        "Special sections:\n"
        "  /playbook coverage     phone-only vs laptop matrix for each scenario\n"
        "  /playbook learning     the incident → postmortem → review loop\n"
        "  /playbook wake         when to wake up at 3am vs sleep through\n"
        "\n"
        "Examples:\n"
        "  /playbook              list all scenarios\n"
        "  /playbook cost         cost ladder triage\n"
        "  /playbook down         pulse health 5xx triage\n"
        "  /playbook deploy       deploy fail triage\n"
        "  /playbook bad card     bad-card-visible scenario"
    ),

    "env": (
        "/env <key> — read a Railway env var on pulse-poc\n"
        "\n"
        "When to use:\n"
        "  - Verify a flag flip (e.g. PULSE_RERUN_ENABLED) landed after a change\n"
        "  - Confirm config before debugging an unexpected behaviour\n"
        "  - Check a kill-switch state without opening the Railway dashboard\n"
        "\n"
        "What it shows:\n"
        "  Current value of the env var on the pulse-poc service. Keys matching\n"
        "  token / secret / key / pass / jwt / api are scrubbed (first 8 chars shown).\n"
        "\n"
        "Examples:\n"
        "  /env PULSE_RERUN_ENABLED\n"
        "  /env PULSE_NEWS_INGEST_ENABLED\n"
        "  /env TELEGRAM_BOT_TOKEN       (scrubbed — shows first 8 chars + ***)"
    ),

    "snooze": (
        "/snooze [kind] [duration] — suppress alert classes temporarily\n"
        "\n"
        "When to use:\n"
        "  - Planned maintenance: silence everything for 1h with /snooze all 1h\n"
        "  - Deploy is expected to fail: /snooze deploy 30m before triggering it\n"
        "  - Feed is intentionally thin during off-peak: /snooze feed 2h\n"
        "\n"
        "Kinds:  cost  deploy  health  feed  all\n"
        "  'all' applies the snooze to every kind at once.\n"
        "\n"
        "Durations:  30m  1h  2h  off  (off = clear immediately)\n"
        "\n"
        "Bare /snooze shows all active snoozes and remaining time.\n"
        "\n"
        "Examples:\n"
        "  /snooze                  show active snoozes\n"
        "  /snooze cost 1h          silence cost alerts for 1 hour\n"
        "  /snooze deploy 30m       silence deploy alerts for 30 minutes\n"
        "  /snooze all 1h           silence ALL alerts for 1 hour\n"
        "  /snooze feed off         clear feed snooze immediately"
    ),
}

# Canonical command names in display order (matches /help listing).
_COMMAND_ORDER = [
    "status", "cost", "breakdown",
    "feed", "cards", "card", "embed",
    "logs", "runbook", "env",
    "snooze",
]


def render(topic: str) -> str:
    """
    Return the help text for a command topic.

    Accepts the command name with or without a leading slash, any case.
    e.g. render("embed"), render("/embed"), render("EMBED") all work.

    Unknown topic → a short "no help for X" message.
    """
    key = topic.lstrip("/").strip().lower()
    text = TOPICS.get(key)
    if text is None:
        return f"no help for '{key}'.  try /help to see commands."
    return text
