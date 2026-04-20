# Pulse — Roadmap

## Product positioning

**Pulse is a standalone, embeddable sports-content widget — built independently, designed to be plugged into operator sites.**

- **Built standalone.** Own repo, own data, own deploy. Runs as a shareable URL out of the box.
- **Embedded in operators** as the primary distribution model. Host site provides the holes; Pulse fills them with content.
- **Operator "holes"** (the integration surface Pulse exposes):
  - Deep-link URL template → every recommendation clicks through to the operator's bet slip (single, combo, or Bet Builder pre-populated).
  - Auth/session token passthrough → operator hands Pulse the user's session so we can personalise and keep the bet slip logged-in.
  - Theme tokens → colors / fonts / radii / logo so Pulse looks native.
  - Scope params → which sport(s), league(s), events to show for this embed.
  - Domain allowlist + embed token → tied to the operator's licence.

**Sport scope:** Soccer only for the MVP.
**Lifecycle scope:** Pre-match only for the MVP. Live comes later.
**Bet types:** Singles, combos (accumulators across events), and Bet Builder recommendations (multi-selection from same event).

**Separate product:** Polybolts (Polymarket tweet bot) stays independent — different repo, different lifecycle.

---

## What survives, what's placeholder

| Component | Status | Plan |
|---|---|---|
| Narrative engine (detector → matcher → scorer → narrator → assembler) | Core IP | Keep and evolve |
| Scripted timelines (LAL / ARS / KC) | Placeholder | Permanent `/demo` route for sales; retired from default path |
| `mock_games.json` / `mock_markets.json` | Placeholder | Replace with Rogue soccer snapshot (Stage 1) |
| `mock_tweets.json` / `mock_players.json` | Placeholder | Replace in Stage 5 (pre-match context source) |
| Mobile WebSocket feed UI | Keep | Already iframe-friendly; themeable in Stage 2 |
| `Card` / `Market` schemas | Evolve | Add multi-selection card type for combos + Bet Builder |

---

## What Rogue gives us (and doesn't)

**Gives us for the MVP path:**
- Pre-match soccer catalogue (~1,820+ fixtures, deep international coverage).
- Real markets with odds — 1X2, AH, O/U, BTTS, correct score, player props (varies).
- `Settings.IsBetBuilderEnabled` at event level — tells us where BB is allowed.
- `/v1/sportsdata/betbuilder/match` — validates proposed BB selection combos.
- Deep-link-friendly IDs (`_id` on events, markets, selections).

**Doesn't give us:**
- Social signals (tweets).
- Rich editorial context (injury news, form narratives, head-to-head storylines).
- Stats beyond what's baked into market offerings.

Context for pre-match storytelling is sourced externally (Stage 5).

---

## Staged plan

### Stage 1 — Foundation: Rogue client + pre-match soccer catalogue *(1–2 days)*

- Port the MCP's `AuthManager` / `apiRequest` / rate limiter to Python as `app/services/rogue_client.py`.
- Boot-time pull: pre-match soccer fixtures from international leagues (EPL, La Liga, Bundesliga, Serie A, Ligue 1, UCL, Europa) via `LeagueId` + `IsTopLeague` filtering.
- For each fixture: deep-scan (`includeMarkets=all`) to get the full market tree and BB eligibility.
- Replace `mock_games.json` / `mock_markets.json` with this snapshot.
- Scripted timelines still run in parallel against mock data, behind `/demo`.
- **Demo unlock:** cards show real pre-match markets + odds for tonight's real soccer fixtures.

### Stage 2 — News-driven recommendation engine + validation surface *(2–3 weeks)*

**Core product insight (north star):** Pulse exists because something has happened in the real world that makes a market, a combo, or a Bet Builder *interesting right now*. The **trigger is news**, not statistics. A news item (injury, team news, transfer, manager quote, tactical story, pre-match preview, post-match reaction, breaking story) arrives → we resolve it to teams/players/fixtures → we find the markets whose interest just changed → we build a candidate card with the news as the hook, the market as the angle, and stats as supporting evidence.

The existing engine in `app/engine/` already encodes this shape — `EventDetector`, `EntityResolver`, `MarketMatcher`, `RelevanceScorer`, `NarrativeGenerator`, `CardAssembler`. The mock cards all follow it: Shams says LeBron is locked in → LeBron points O/U card. Saka returning from injury → Saka goalscorer card. What Stage 2 adds is a **real news pipeline** feeding that engine, a **candidate store** between engine and feed, a **policy layer** for scoring / dedupe / volume control, and a **validation surface** for human review.

**Architecture**

```
┌───────────────────┐   ┌─────────────────┐   ┌───────────────────┐
│ News ingestion    │──>│ Entity resolver │──>│ Relevance gate    │
│ (feeds, articles, │   │ (teams /        │   │ (does any market  │
│  social, LLM,     │   │  players /      │   │  care about this  │
│  operator press)  │   │  fixtures)      │   │  news?)           │
└───────────────────┘   └─────────────────┘   └─────────┬─────────┘
                                                        ▼
                                              ┌───────────────────┐
                                              │ Market matcher    │
                                              │ (single / combo / │
                                              │  BB candidates)   │
                                              └─────────┬─────────┘
                                                        ▼
                          ┌────────────────────────────────────────┐
                          │ Candidate store                        │
                          │ (every candidate, scored, with reason) │
                          └────────┬────────────────────┬──────────┘
                                   ▼                    ▼
                          ┌────────────────┐   ┌────────────────────┐
                          │ Policy / rank  │   │ /admin/candidates  │
                          │ / volume       │   │ table — review,    │
                          │ controls       │   │ approve, label,    │
                          └───────┬────────┘   │ tune thresholds    │
                                  ▼            └────────────────────┘
                          ┌────────────────┐
                          │ Public feed    │
                          │ (user-facing)  │
                          └────────────────┘
```

**What Stage 2 specifically builds**

1. **News-ingestion layer.** A `NewsIngester` service that pulls real-world sports news into a normalised `NewsItem` schema (headline, body, source, timestamp, mentions, type). Triggers:
   - *Articles / editorial* — ESPN, BBC, Sky, The Athletic (LLM + web search is the cheapest v1; dedicated sports news APIs later).
   - *Team news* — injury updates, starting XI leaks, pressers, tactical tweaks, suspensions.
   - *Transfers* — confirmed / rumoured moves affecting price-relevant players.
   - *Social signal* — verified reporters (Shams, Schefter, Fabrizio Romano, Ornstein style). Likely LLM-assisted topic classification over an X/RSS/Bluesky stream.
   - *Operator-originated* — price moves, new markets opening, early-payout triggers, suspensions (these are "the market itself has news"; produced from Rogue + Rogue SSE in Stage 6/7 but the ingester shape is the same).
2. **Entity resolution.** Reuse / extend `app/engine/entity_resolver.py` — map each news item's mentions to Rogue entity IDs (team, player, fixture, league).
3. **Relevance gate.** Reject news that touches no market in the current Rogue catalogue. If the Saka news story breaks but no Saka-related market exists, skip.
4. **Market / combo matcher.** Extend `MarketMatcher` so a single news item can produce:
   - *Single*: the primary market the news directly affects.
   - *Bet Builder*: same-event multi-leg if the story supports it (e.g. Saka fit → Saka scorer + Arsenal win + O2.5 → validated via `/v1/sportsdata/betbuilder/match`).
   - *Combo*: cross-event accumulator when several same-weekend news items compound (e.g. three different team-news stories all pointing to heavy favourites).
5. **Candidate store.** New model/table (`CandidateCard`) persisting every candidate whether it publishes or not. Fields: news source(s), hook type, affected entities, market(s) / legs, score, threshold decision, reason, generated_at, expires_at, status (draft / queued / published / rejected / expired), narrative draft.
6. **Policy layer.** Scoring uses the existing `RelevanceScorer` shape but with news-first weights:
   - News quality (source credibility, recency, exclusivity)
   - Entity market-coverage (does the story touch a liquid market?)
   - Price-movement corroboration (odds shift after the news = strong signal)
   - Stats support (form / h2h / xG / career numbers back the angle)
   - Fixture proximity (kickoff within the next N days)
   Plus global rules: per-fixture dedupe, per-embed volume caps, per-source rate-limit, theme/BB exclusivity.
7. **Validation surface — `/admin/candidates`.** Table of every candidate from the last run. Columns: timestamp, source, headline, hook type, teams, market/legs, odds, score, threshold ✓/✗, reason, status, narrative draft. Filters: hook type, league, status, date. Default: above-threshold; toggle shows all. Approve-override / reject-override with a reason code → labelled dataset to tune scoring. Counts dashboard: candidates generated today, published, rejected, by hook / source.

**Target volume.** Hundreds of candidates/day across all fixtures is the healthy engine output. The public feed shows the top slice (per user / per embed), ranked and rate-limited. Live betting will multiply candidate volume when it lands.

**What Stage 2 is NOT**
- Not a schedule-driven "curated picks" product. Themes/combos are generators *inside* the engine, not the primary output.
- Not a stats-first product. Stats are card supporting evidence, never the trigger.
- Not the embed/widget delivery layer — that's Stage 5, after the content is good enough to embed.

### Stage 3 — Combo + Bet Builder recommender *(1–2 weeks)*

Builds inside the Stage 2 engine as a specialised candidate generator. A news hook that affects multiple related markets produces a BB or combo candidate instead of (or in addition to) a single. Scheduled theme-based combos run as secondary generators when news density is low.

- **`ComboBuilder`** proposes:
  - **News-triggered Bet Builders** — e.g. Saka fit → Saka scorer + Arsenal win + O2.5. Validated via Rogue `/v1/sportsdata/betbuilder/match`.
  - **News-triggered accumulators** — compound stories across same-weekend fixtures.
  - **Scheduled curated themes** (fallback when news is thin):
    - *Weekend Safe Lean* — 2 legs, combined odds 2.00–4.00.
    - *Value Long Shot* — 4–5 legs, 8.00+.
    - *Goal-fest* — 3 legs, O2.5 + BTTS stacks, 3.00–6.00.
    - *Derby Bet Builder* — same-event BB on a high-profile fixture.
    - *Top-League Treble* — 3 legs from EPL/La Liga/UCL 1X2 picks.
- **New card type** — multi-leg, stacked selections, total odds, single deep-link CTA.
- **Deep-link shapes** — operator config supports separate templates for single / combo / BB.

### Stage 4 — Stats / supporting-evidence layer *(1 week)*

Stats are *evidence*, never trigger. This stage makes them richer so narratives land harder.

- `StatsEnricher` service — given a fixture + hook, pull relevant recent form / xG / h2h / career numbers.
- Sources: LLM + web search v1; dedicated stats feed (Opta / SofaScore-style) later.
- Cache per fixture for the day.
- Feeds `NarrativeGenerator` and the card's `stats[]` array.

### Stage 5 — Embed delivery (the "holes") *(1 week)*

Ship this once the content is good enough to sell. Up to here Pulse is a standalone URL.

- **URL-param scoping** — `?sport=soccer&leagues=epl,ucl&operator=apuesta-total&theme=dark`.
- **Embed modes** — iframe (MVP), JS snippet / shadow DOM (later).
- **Deep-link template system** — per-operator config stores URL patterns for single / combo / BB bet-slip handoff.
- **Theme tokens** — CSS variables (colors / fonts / radius / logo).
- **Auth token passthrough** — URL param or `postMessage`; stored for session; included on deep-link clicks. Validation stub until Stage 8.
- **CORS + domain allowlist** — embed tokens tied to licensed operator domains.
- **Admin config** — minimal YAML/JSON per operator for v1. Proper dashboard later.

### Stage 6 — Live pulse: SSE odds overlay *(2–3 days)*

- Subscribe to Rogue SSE for in-play events inside the embed's scope.
- Piped odds updates feed the candidate engine as a new hook type (*price move*) and update displayed prices on live cards.
- Cards de-list if markets close / teams suspend.

### Stage 7 — Live event detection *(~1 week)*

- `SSEEventDetector` for score changes, suspensions, odds moves, new markets.
- Live-specific candidate hooks: goal scored, red card, suspension, momentum shift, line moved, market opened, early-payout triggered.

### Stage 8 — Operator auth handoff *(1–2 weeks, pending operator docs)*

- Validate tokens against operator-provided endpoints / public keys.
- Exchange for internal Pulse session; personalise feed; pass user context through on deep-links.
- Blocks on operator documentation — Ian to source.

### Stage 9 — Multi-operator *(later)*

- Abstract `RogueClient` behind a `SportsbookClient` interface.
- Add adapters for other providers (Kambi / Altenar / Betconstruct).
- Widget config picks which sportsbook backs the feed.

---

## MVP definition

**"Pre-match soccer Pulse — news-driven feed, singles + combos + Bet Builder, with an admin validation surface, embedded in one operator."**

That's Stages 1 → 5. Everything after is enhancement.

---

## Decisions made

1. **Built standalone, embedded into operators.** Pulse is its own product; operators are distribution channels.
2. **News is the trigger; stats are supporting evidence.** Every card answers "what just happened that makes this market interesting?" Not the reverse.
3. **Soccer only for MVP.** Other sports deferred.
4. **Pre-match only for MVP.** Live deferred to Stage 6+.
5. **Singles + combos + Bet Builder** all in MVP scope. BB validation via Rogue `/betbuilder/match`.
6. **Content scope: international leagues.** EPL, La Liga, Bundesliga, Serie A, Ligue 1, UCL, Europa.
7. **Demo mode permanent.** Scripted LAL/ARS/KC lives at `/demo` forever.
8. **Monetization: fixed embed licence.** No click attribution tracking needed.
9. **Validation surface in-app.** `/admin/candidates` table. V1 is read-only — columns + filters, no approve/reject buttons. Approve/reject land in a later pass once we've watched the engine for a while.
10. **Volume target:** hundreds of candidates/day engine-side; published feed is a top-slice, ranked and rate-limited per embed.
11. **News-ingestion v1:** LLM + web search per upcoming fixture ("what's news on these teams/players in the last 24h?"). No long-running RSS pipeline for v1. Cost budget + model choice TBD.
12. **Candidate store:** SQLite file in the backend (zero new infra). Migrate to Postgres later if needed.

## Still open

- **Entity resolution coverage** — how deep on squad/player coverage before the matcher can reliably connect news to markets?
- **LLM model + budget** — Haiku for ingestion vs. larger model for narrative generation; target cost per fixture per day.
- **Engine cadence** — how often does the ingest-score-store loop run (every 15 min? every hour? on-demand only)?
- **Operator docs** — Ian to source; blocks Stage 8.

---

## Current status

- **Stage 2 — DONE locally (2026-04-20).** News-driven recommendation engine + validation surface running end-to-end.
  - Schemas + SQLite store: `app/models/news.py`, `app/services/candidate_store.py`.
  - News ingester: `app/services/news_ingester.py` (Claude Haiku 4.5 + native web search + terminal `submit_news_items` tool, prompt-cached system prompt). Mock fallback `app/services/mock_news_ingester.py` (20 curated stories) when `ANTHROPIC_API_KEY` is not set.
  - Entity resolver: `app/engine/news_entity_resolver.py` — builds alias map from live Rogue catalogue at boot; mentions -> `fixture_ids` + `team_ids`.
  - Candidate builder: `app/engine/candidate_builder.py` — hook_type drives market priority (1X2 primary, O/U or BTTS for tactical / injury hooks).
  - Scorer + policy: `app/engine/news_scorer.py` — weighted (news_quality 0.30, coverage 0.20, proximity 0.20, hook 0.20, stats 0.10). Policy does per-(fixture,hook) dedupe, per-fixture cap (default 3), publish threshold `PULSE_PUBLISH_THRESHOLD=0.55`.
  - Orchestrator: `app/services/candidate_engine.py`. Run-once pipeline invoked at boot in Rogue mode.
  - Admin surface: `GET /admin/candidates` (HTML table with filters + toggle for above-threshold-only) and `GET /admin/candidates.json`. Counts-by-hook × status summary at the bottom.
  - Published candidates overlay the public pre-match feed on top of Stage 1's baseline 1X2 card per fixture.
  - **Verified via live smoke:** 10 real Rogue fixtures → 67 LLM-scouted candidates (18 injury, 21 team_news, 8 manager_quote, 10 preview, 7 tactical, 3 article) → 59 published, 8 rejected by policy cap → 69 cards in public feed.
  - **Known rough edges surfaced by the admin table** (exactly what it's for): loose entity match occasionally cross-posts news from a mentioned team to a different fixture featuring the same team; narrative is the raw headline (Stage 4 will rewrite via LLM); scores cluster 0.7–0.8 with little spread (scorer weights need tuning once we have more data).
- **Stage 1 — DONE locally (2026-04-20).** Real Rogue soccer fixtures render end-to-end through the existing mobile UI.
  - `RogueClient` (Python port of the TS MCP): `backend/app/services/rogue_client.py`.
  - Catalogue loader: `backend/app/services/catalogue_loader.py`. Pulls top-league soccer fixtures from Rogue, filters to international leagues (EPL / La Liga / Bundesliga / Serie A / Ligue 1 / UCL / Europa), deep-scans `includeMarkets=default`, maps to internal `Game`/`Market` types.
  - Pre-match cards: `backend/app/services/rogue_prematch.py`. Stage 1 is one card per fixture with the 1X2 market. Themes + combos land in Stages 3/4.
  - Switch via `PULSE_DATA_SOURCE=rogue|mock` (default `mock`). `ROGUE_CONFIG_JWT` required.
  - Verified: 10 real fixtures, real odds, Home/Draw/Away ordering correct, Total Goals O/U main-line selection logic working.
- **Known cosmetic debt** (defer to Stage 2/5):
  - Top filter chips still read `NBA / NFL / EPL` — need replacing with soccer-league chips when we finalize the sport scope.
  - Two-word team avatars (e.g. Crystal Palace) render as two separate letters; look wrong for long club names.
  - Narrative copy is placeholder ("Italy - Serie A — Lecce host Fiorentina …"). Gets rewritten in Stage 5 by the `ContextEnricher`.
  - Relevance score hardcoded 0.5. Real scoring arrives with Stage 3 singles recommender.
- **Not yet deployed** to Railway. Need to add `PULSE_DATA_SOURCE` and `ROGUE_CONFIG_JWT` as env vars there before the Railway build serves real data.

- Rogue API MCP available at `/Users/ianbradley/Downloads/rogue-api-mcp/` — reference for the Python port.
