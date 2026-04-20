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

### Stage 2 — Embed hooks (the "holes") *(3–5 days)*

Ship the integration surface before building more content on top.

- **URL-param scoping** — `?sport=soccer&leagues=epl,ucl&operator=apuesta-total&theme=dark`.
- **Embed modes** — iframe (MVP), JS snippet / shadow DOM (later).
- **Deep-link template system** — operator config stores a URL pattern; every card's CTA renders through it. Supports single-bet, combo, and Bet Builder link shapes. Per-operator.
- **Theme tokens** — CSS variables for colors / fonts / radius / logo, driven by operator config.
- **Auth token passthrough** — accept a token via URL param or `postMessage`; store for session; pass through on deep-link clicks (pre-populated bet slip). Validation is operator-specific — stubbed until Stage 7 docs arrive.
- **CORS + domain allowlist** — embed tokens tied to licensed operator domains.
- **Admin config** — minimal YAML/JSON config file per operator for v1 (licence, domains, deep-link template, theme). Proper dashboard later.

### Stage 3 — Singles recommender (narrative-driven pre-match cards) *(1 week)*

Reshape the existing engine for pre-match singles on real soccer fixtures.

- `PreMatchEventDetector` — generates angle-based events from Rogue data + context:
  - Value line detected (odds favourably moved vs market consensus)
  - Form streak (from enriched context — comes online in Stage 5)
  - Top league fixture of the day
  - BB-enabled high-interest match
- `MarketMatcher` extended to handle Rogue's full market tree (AH, BTTS, player props).
- `NarrativeGenerator` reworked for soccer-specific angles.
- Cards show a single market, a reason, and a deep-link CTA.
- **Demo unlock:** a real pre-match feed. Ugly narrative until Stage 5, but real data end-to-end.

### Stage 4 — Combo + Bet Builder recommender *(1–2 weeks)*

The new product wedge. Separate engine component.

- **`ComboBuilder` engine** that proposes:
  - **Same-game Bet Builders** — multiple selections from one event. Validated via Rogue `/v1/sportsdata/betbuilder/match` before offering to the user (drops combinations the book rejects).
  - **Cross-event accumulators** — 2–4 legs from different fixtures in the day's scope.
- **Selection logic — curated themes only for v1.** Each theme is a named editorial slot with its own rules. Starter set:
  - *Weekend Safe Lean* — 2 legs, short-priced favourites, combined odds 2.00–4.00.
  - *Value Long Shot* — 4–5 legs, combined odds 8.00+.
  - *Goal-fest* — 3 legs across events, O2.5 + BTTS stacks, 3.00–6.00.
  - *Derby Bet Builder* — same-event BB on a high-profile fixture, 3 legs.
  - *Top-League Treble* — 3 legs from EPL/La Liga/UCL 1X2 picks.
- **Leg count is per-theme**, not global. Easy to add/remove themes without reshaping code.
- Automation can backfill once we have real running data; defer.
- **New card type** — multi-leg card with stacked selections, total odds, and a single deep-link CTA that loads the full combo into the operator's bet slip.
- **Deep-link shapes** — operator config supports separate templates for single / combo / BB (different operators assemble bet slips differently).
- **Demo unlock:** combo + BB recs alongside singles. The "why would I use Pulse instead of just opening my sportsbook" answer.

### Stage 5 — Pre-match context enrichment *(1–2 weeks)*

**Source: LLM + web search at card-assembly time.** Cheapest POC route, lowest pipeline overhead.

- New `ContextEnricher` service — takes fixture details (teams, league, kickoff) and returns a structured blob: recent form, injury news, head-to-head note, hook sentence.
- Called once per fixture at card-assembly time; cached on the fixture ID for the day (avoid repeated API spend).
- Feeds `NarrativeGenerator` (card copy) and `ComboBuilder` theme selection (e.g. injury news → skip that fixture in "safe lean").
- Model + prompt TBD; measure cost per card early and budget accordingly.

### Stage 6 — Live pulse: SSE odds overlay *(2–3 days, post-MVP)*

- Subscribe to Rogue SSE for the in-play events inside the embed's scope.
- Piped odds updates mark cards as "line moved" and update the displayed prices.
- Cards can de-list if markets close / teams suspend.

### Stage 7 — Live event detection *(~1 week, post-MVP)*

- `SSEEventDetector` for score changes, suspensions, odds moves, new markets.
- Live-specific card types: momentum, line-moved, market-opened, early-payout.

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

**"Pre-match soccer Pulse, embedded in one operator, recommending singles + combos + Bet Builder."**

That's Stages 1 → 5. Everything after is enhancement.

---

## Decisions made

1. **Built standalone, embedded into operators.** Pulse is its own product; operators are distribution channels.
2. **Soccer only for MVP.** Other sports deferred.
3. **Pre-match only for MVP.** Live deferred to Stage 6+.
4. **Singles + combos + Bet Builder** all in MVP scope. Bet Builder validation via Rogue `/betbuilder/match`.
5. **Content scope: international leagues.** EPL, La Liga, Bundesliga, Serie A, Ligue 1, UCL, Europa.
6. **Demo mode permanent.** Scripted LAL/ARS/KC lives at `/demo` forever.
7. **Monetization: fixed embed licence.** No click attribution tracking needed.

## Still open

- **Cards per day / slot allocation** — how many Pulse cards show per embed per day, and how are theme slots filled (one-of-each-theme, or volume-weighted)?
- **LLM budget** — acceptable cost per card in Stage 5; model choice (Haiku for enrichment?).
- **Operator docs** — Ian to source; blocks Stage 8.

---

## Current status

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
