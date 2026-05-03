# Pulse — fixture importance + market depth

**Status:** Design doc, 2026-04-29. Phase 1 shipped 2026-04-29 (PR #108) and calibrated against live Apuesta Total data 2026-04-30 — see "Phase 1 calibration" below. Open questions inline (search `OPEN:`).

## What this fixes

Today the engine treats every fixture roughly the same. A UCL semi-final and a midweek La Liga clash get the same `PULSE_NEWS_MAX_SEARCHES=2`, the same hardcoded MarketCatalog whitelist (~6 markets), the same freshness window. The output for a marquee fixture (e.g. AM vs ARS, 2026-04-29) is 5 cards, 2 narrative angles, 6 markets sampled — out of 396 active markets the operator exposes for that single fixture.

The product gap: **importance is contextual.** What matters to a Peru-licensed operator (Liga 1 Peru, Copa Libertadores, Copa Sudamericana, then UCL) is not what matters to a UK-licensed operator (Premier League, then UCL, then FA Cup). What matters in October ≠ what matters in May knockout rounds. A "huge game" is huge to whom?

The fix: an **importance-weighted engine** that goes deep on fixtures that matter (per the operator's audience and the calendar phase) and stays cheap on the rest.

---

## Framing — localized relevance

Not localization of language. Localization of **what matters**. Same product, different relevance frame per operator.

Each operator's iframe should see a feed where:
- Fixtures their audience cares about get deeper coverage (more narrative angles, broader markets, tighter freshness)
- Fixtures their audience doesn't care about are filtered out before any LLM cost
- The operator doesn't have to declare anything — Rogue already exposes their merchandising team's daily decisions

---

## Eight signals and where they come from

| Dimension | Source | Role | Status |
|---|---|---|---|
| Operator commercial focus (today's featured fixtures) | Rogue MCP `get_featured` / `EventsController_getFeaturedEvents` | **Ranker** (binary discriminator) | Phase 1: captured |
| Operator commercial promo (cashout, boosts) | Rogue event field `IsEarlyPayout` + `EarlyPayoutValue` | Filter only — *not a useful ranker, see calibration below* | Phase 1: captured |
| Competition prestige (per operator) | Rogue event field `LeagueOrder` (lower = higher) | **Ranker** (numeric, well-distributed) | Phase 1: captured |
| Operator top-league flag | Rogue event field `IsTopLeague` | Filter only — already used at catalogue stage; redundant for ranking | Phase 1: captured |
| Competition tier baseline (global) | Our `leagues` reference table (TBD) | Filter floor + tiebreaker | Not built |
| Calendar phase (knockout, title race, relegation) | Our config + already-fetched standings | **Multiplier** | Partial (standings cached) |
| Fixture intrinsics (derby, knockout escalator) | Our derby table + Rogue `LeagueGroupId` / stage hints | **Multiplier** | Not built |
| Market depth per fixture | Rogue `MarketGroups` + `TotalActiveMarketsCount` per event | Engine knob in deep mode | Phase 4: not built |
| Audience signal (engagement-weighted) | Pulse's own reactions + click telemetry | Long-term feedback loop | Wave 5+ |

**Phase 1 calibration changed the role of two signals.** See the "Phase 1 calibration" section before reading the score formula below.

### What the data actually looks like — Apuesta Total, 2026-04-29

`get_featured` returned 27 events. Composition:

| Competition | Featured fixtures |
|---|---|
| Copa Libertadores | 9 |
| Copa Sudamericana | 9 |
| Europa League | 2 |
| Europa Conference League | 2 |
| Champions League | 1 |
| Premier League | 1 |
| CONCACAF Champions Cup | 1 |
| Other | 2 |

Apuesta Total's merchandising team is telling us: South American continental tournaments are 2-3× more important to their Peru/Latam audience than tonight's UCL semi-final by featured-volume. Refreshed continuously, free, no operator-side config needed.

`get_event(id=AM-vs-ARS, includeMarkets=none)` returned:

```
IsTopLeague:               true
LeagueOrder:               1000001          (lowest = highest priority on Apuesta Total)
IsEarlyPayout:             true             (operator-funded cashout — commercial promo)
EarlyPayoutValue:          2
TotalActiveMarketsCount:   396              (we currently sample 6)
MarketGroups:              ["Main", "Halves", "Goals", "Corners", "Players", "Cards", "Specials", "Period"]
Settings.IsBetBuilderEnabled: true
```

The `LeagueOrder` ladder for Apuesta Total today (lower number = higher commercial priority):

```
UCL                  1,000,001
Europa League        3,000,001
Europa Conference    4,000,001
Copa Libertadores    6,000,001
Copa Sudamericana    6,000,002
Premier League       7,000,002
CONCACAF Champions  64,001,601
```

UCL is ranked highest by **competition prestige** (LeagueOrder = 1M tier). But the **featured list** skews heavily Latam. These are two different operator signals — both useful, used differently in the score.

---

## Phase 1 calibration (2026-04-30)

Phase 1 (PR #108) shipped 2026-04-29 18:18 UTC. First live boot log:

```
[catalogue] tagged 4 fixtures as operator-featured (out of 10 total catalogued)
[catalogue] importance signals: featured=4, top_league=10, early_payout=10
            (league_order range: [1000001, 12000001], n=10)
```

What this taught us about each signal's actual discriminating power on Apuesta Total's catalogue:

| Signal | Coverage in catalogue | Useful for ranking? |
|---|---|---|
| `is_top_league` | 10/10 | **No** — already used as catalogue filter; every Game in the engine has this true |
| `is_early_payout` | 10/10 | **No** — Apuesta Total enables it on every catalogued fixture |
| `is_operator_featured` | 4/10 | **Yes** — clean 40/60 split |
| `LeagueOrder` | 1M–12M range across 10 fixtures | **Yes** — numeric, well-distributed |

The two booleans (`is_top_league`, `is_early_payout`) are saturated within the catalogue. They were captured in Phase 1 anyway — useful as observability + future-proof if Apuesta Total ever stops being so generous with `IsEarlyPayout` — but the score formula below uses only the two discriminating signals.

**Lesson**: design-time score formulas are guesses until calibrated against production data. Always capture more signals than you score, then trim by observation. Same family of lesson as `feedback_check_upstream_first.md` — the world is more concrete than the spec lets you assume.

---

## Importance score

For each (embed, fixture) pair, compute a score in `[0, 1]`:

```
operator_signal = max(
  1.0   if fixture in operator's getFeaturedEvents,
  1.0 - (LeagueOrder / 100_000_000)         # normalized inverse-rank, prestige signal
)

score = operator_signal
        × calendar_phase_factor             # ours, defaults 1.0
        × intrinsic_score                   # ours (derby, knockout stage), defaults 1.0
```

The `max(...)` of two operator signals means a fixture qualifies for "operator says this matters" if **either**:
- It's in today's featured events (commercial focus, the binary discriminator), OR
- Its competition is ranked very high by `LeagueOrder` (prestige, the numeric ladder)

Operator manual boost is **already covered** by the featured signal — operators promote fixtures via Rogue's own featured-events API, which we now consume. No Pulse-side override surface needed for v1.

`is_early_payout` and `is_top_league` are intentionally absent from the formula — see the Phase 1 calibration above.

That score routes the fixture via **gradient routing** (Phase 2b, shipped 2026-05-01, PR #112). The score is a continuous multiplier — no buckets, no thresholds:

- `max_searches`, `per_fixture_cap`, and `max_cost_usd` each interpolate linearly between a configured floor (at score=0) and ceiling (at score=1) via `gradient_factor()` in `app/engine/importance_scorer.py`.
- A fixture with `importance_score=1.0` (operator-featured or rank-0 LeagueOrder) gets the ceiling for every knob; a fixture with `importance_score=0.1` gets ~10% of the way from floor to ceiling.
- No `*_DEEP_THRESHOLD` env var to tune. Cost-safe by design — per-fixture LLM cap bounds spend even for top fixtures.

Earlier bucket text (`>0.7 deep / 0.4-0.7 standard / <0.4 minimal`) is **deprecated** and survives only in `classify_score()` for log-line back-compat. Don't add new code paths that depend on the buckets.

---

## Per-operator vs global engine

For MVP: **one global engine pass, per-operator feed filter.**

- Engine scouts the **union** of all operators' interests. Any fixture that's in deep-mode for ANY operator gets deep-scouted globally. Cost: O(distinct fixtures across all operators), not O(operators × fixtures).
- Cards land in a shared `published_cards` table.
- Each operator's `/api/feed` filters and re-ranks per its own embed-level weights. So Apuesta Total's iframe leads with Libertadores cards; a UK operator's iframe (when we have one) leads with Premier League.

Long-term (5+ operators with conflicting interests, justifying it): per-operator engine passes. Not for v1.

---

## Engine integration — four steps

The four PRs that implement the above. Each is shippable independently; each is small.

### PR 1 — Capture the signals (foundation) — SHIPPED 2026-04-29 (PR #108)

**Files:** `backend/app/services/rogue_client.py`, `backend/app/services/catalogue_loader.py`, `backend/app/models/news.py` (or wherever `Game` lives).

- Extend `Game` pydantic with new fields: `league_order: Optional[int]`, `is_early_payout: bool`, `early_payout_value: Optional[float]`, `region_code: Optional[str]`, `is_top_league: bool`, `league_group_id: Optional[str]`, `is_operator_featured: bool` (the last computed, not from a single field — see PR 2).
- Extend `_normalize_event` in `RogueClient` to read these from the bulk `multilanguage/events` response and stamp them onto `Game`.
- No engine logic changes. No score yet. Just plumbing.
- Schema-drift discipline: `Game` is currently in-memory only (`feed.prematch_cards`); it's not persisted to a SQLite table by default. Confirm before merge — if any persistence path serialized Game, we follow the 4-site pattern.

**Acceptance:** boot logs show one fixture's `league_order=1000001 is_early_payout=true` etc. No behaviour change.

### PR 2 — Add `RogueClient.get_featured_events()` + tag fixtures — SHIPPED 2026-04-29 (folded into PR #108)

**Files:** `backend/app/services/rogue_client.py`, `backend/app/services/catalogue_loader.py`.

- Add `RogueClient.get_featured_events(locale: str = "en") -> list[dict]` mirroring the existing `get_featured_betbuilders` pattern.
- Cache the result for the duration of one tier cycle (free Rogue call, but no point re-fetching it inside one cycle).
- In the catalogue loader, after fetching the bulk events, also fetch featured events. Mark each `Game.is_operator_featured = True` if its `_id` appears in the featured set.

**Acceptance:** `cards_in_feed_now > 0` cards have `is_operator_featured=true` on the live feed; the AM vs ARS fixture is one of them.

**This alone gives us the strongest single importance signal — operator-curated featured set — with no other engineering.**

### PR 3 — Importance scorer + tier router

**Files:** `backend/app/engine/importance_scorer.py` (new), wherever the tier loop hands fixtures to `_run_candidate_engine`, plus the `embeds` table for per-operator config.

- New `ImportanceScorer` class with `score(game, embed) -> float`. Pure function over the signals captured in PR 1+2.
- Add to `embeds` table: `jurisdiction TEXT` (e.g. `"PE"`), `competition_floor REAL` (default `0.4` — drops fixtures below this score from this embed's feed). Schema-drift discipline.
- New `leagues` reference table — hand-curated list of ~100 leagues with `competition_tier` (1=top, 2=second, etc.) and a `default_weight`. One-time data import, then static.
- Tier loops route fixtures to `deep`/`standard`/`minimal` modes based on score (initially using a simple global threshold; later per-operator).
- Storyline detector + news scout read the mode and adjust knobs (more searches in deep mode, etc.).
- Per-fixture LLM budget cap so a deep fixture can't blow the day budget alone.

**Acceptance:** AM vs ARS scores `> 0.9` for Apuesta Total → deep mode → next cycle does an aggressive scout pass; Cusco FC vs random scores `< 0.3` → minimal mode → no scout.

### Phase 3 — Market depth expander (singles + BB + combos, customer-facing)

**Reframe (2026-05-03):** market depth is a **customer offering**, not just an engine knob. The deliverable is rich, varied bet content per top-importance fixture — across all three bet-construction surfaces (singles, Bet Builders, combos) — that the operator can hold up as a differentiator vs. their default sportsbook front page. Phase 2b's gradient routing is the cost-control mechanism that makes this safe.

**Upstream-first finding (HR1):** the Rogue OpenAPI spec exposes per-market `MarketGroupOrder: number` and `InMarketGroups: array` on `MarketWithSelectionsMultiLanguageModel`. The operator's merchandising team already orders markets within each group (`Goals`, `Corners`, etc.) — we don't invent a "narrative-fit scoring" system. Sample by `MarketGroupOrder` ascending per group; that IS the operator's curation signal.

This resolves the prior open question on `MarketCatalog.ALLOW`: it stays as the **floor** (1X2 / OU 2.5 / BTTS guaranteed regardless of group composition); upstream-ordered groups add markets on top.

#### Phase 3a — Capture and observe (this PR, no behaviour change)

**Files:** `backend/app/services/market_depth_observer.py` (new), `backend/app/services/catalogue_loader.py`, `backend/app/main.py` (admin endpoint).

- For top-N gradient fixtures per cycle, fetch `event?id=...&includeMarkets=all` (path already exists in `RogueClient.get_event`).
- Per-fixture log a rich report: `TotalActiveMarketsCount`, `Settings.IsBetBuilderEnabled`, per-group market count + `MarketGroupOrder` min/max/median, current vs. available coverage for singles / BB / combo surfaces.
- New admin endpoint `/admin/market-depth/<event_id>` returning the same report as JSON for ad-hoc poking.
- Behind kill-switch `PULSE_MARKET_DEPTH_OBSERVE_ENABLED` (default `true` — pure observability, no LLM cost, only N free Rogue calls per cycle).
- No persisted fields. Pure logging + on-demand endpoint.

**Acceptance:** boot logs show one line per top-3 fixture with full per-group `MarketGroupOrder` distribution. After 1-2 cycles, we have the data to set per-group caps in 3b.

#### Phase 3b — Route on the data (next PR)

- Singles: per-group cap = `gradient_factor(score, floor_per_group, ceil_per_group)`. Pull markets sorted by `MarketGroupOrder` ascending, take top-N per group, union with `MarketCatalog.ALLOW` floor.
- Bet Builders: same upstream ordering used to seed BB combinatorics within high-importance fixtures (Phase 3c).
- Combos: cross-event combo builder (`backend/app/engine/cross_event_builder.py`) gets a wider market pool to draw from on top fixtures.
- Kmianko bscode mint regression for any market type new to deep-link minting (per the deep-link QA gap memory).

#### Phase 3c — Customer-facing offering

- Per-operator config knob (default ON for AT): "show market depth badge" — UI affordance on cards built from deep-pool markets, telegraphing breadth.
- Operator-facing one-line claim: "Top fixtures get 5–8× more market variety than the static catalogue." Calibrate the multiplier with Phase 3a data before publishing.

**Files affected at 3b/3c:** `backend/app/engine/candidate_builder.py`, `backend/app/engine/combo_builder.py`, `backend/app/engine/cross_event_builder.py`.

#### Appendix — Phase 3b cap-by-gradient design (PR #119)

Phase 2b's `gradient_factor()` already scales `max_searches`,
`per_fixture_cap`, and `max_cost_usd` linearly by `importance_score`.
PR #119 adds a fourth gradient knob — **per-group market cap** — sized
the same way:

| Score | Per-group market cap | Notes |
|---|---|---|
| 1.0 (top) | 10 | Top fixture (Man U vs LIV scale) — saturates the BB-eligible pool |
| 0.5 (mid) | 6  | Linear interpolation between floor / ceiling |
| 0.0 (tail) | 2 | Tail fixture — keeps cost down, ALLOW floor still surfaces |

`MarketCatalog.ALLOW`-equivalent floor markets (1X2, OU 2.5, BTTS, AH,
DNB, double-chance) survive at every score — they are the anchor
markets every card needs. Pool builder is
`app/engine/market_pool_builder.py::build_pool`.

Two groups skipped by default: `All Markets` (every market belongs;
including would bypass per-group caps) and `Special` (operator marks
`MarketOrder=999999` — unranked, sampling would be random).

#### Appendix — BB diversity targets (PR #119)

Once the pool is broader, we want BBs to actually USE the new variety
— not just pick the same 2–3 leg combos forever. Two diversification
axes, both observed first, then routed on:

**Leg-count target distribution per importance:**

| Score | 1-leg | 2-leg | 3-leg | 4-leg | 5-leg | 6-leg |
|---|---|---|---|---|---|---|
| 1.0 (top)  | 20% | 20% | 25% | 20% | 10% | 5% |
| 0.0 (tail) | 50% | 30% | 20% | 0%  | 0%  | 0% |

**Odds-bucket target distribution per importance:**

| Score | short [1.00–1.50) | mid [1.50–2.50) | plus [2.50–5.00) | long [5.00–12.00) | lottery [12.00+) |
|---|---|---|---|---|---|
| 1.0 (top)  | 15% | 35% | 30% | 15% | 5% |
| 0.0 (tail) | 30% | 50% | 15% | 5%  | 0% |

These are starting points, not committed targets. Phase 3b PR #119
ships `composition_report()` in `app/engine/bb_diversity.py` plus a
cycle-end `[pool_composition]` log line so we can compare actual vs.
target across 1–2 cycles before tuning. Routing the engine to *hit*
the targets lands in PR #120 (which themes / legs to favour when
actual distribution is below target).

#### Appendix — Composition observability log (PR #119, always on)

End of every CandidateEngine cycle now emits:

```
[pool_composition] total=47  bet_type={single=18, bet_builder=21, combo=8}
                   legs={1=18, 2=12, 3=11, 4=4, 5=2, 6=0}
                   odds_bucket={short=4, mid=22, plus=15, long=5, lottery=1}
                   odds_range=[1.32, 14.50]
```

No env knob — pure observability, wrapped in try/except so it never
breaks the cycle. Phase 3b's calibration question is answered by
reading this line across a few cycles, then deciding whether to flip
the route layer (`PULSE_DEEP_MARKET_EXPANSION_ENABLED=true`) and the
diversity targeter (PR #120).

---

## Market-depth expander (own workstream)

This is Phase 3 above but worth calling out separately because it's the most operator-visible improvement and has its own dependencies:

- **`MarketGroupOrder` per market is the source of truth** — the operator's merchandising team has already ranked markets within each group (`Goals`, `Corners`, `Players`, etc.). We sample top-N per group by ascending `MarketGroupOrder`, union with `MarketCatalog.ALLOW` as the floor. We don't invent a "narrative-fit" scoring system (HR1: don't reinvent what's upstream).
- Each market type that's new to us needs:
  - Renderer support (already covered for most via the leg/selection schema)
  - Selection-id format check (the Goalscorer-2+ gotcha says some markets lack `player_id` in the selection_id — those should be filtered)
  - Kmianko bscode mint regression test (browser-verified, not curl)
- Phase 3a (observe) ships behind `PULSE_MARKET_DEPTH_OBSERVE_ENABLED=true` (default on, pure observability, no LLM cost).
- Phase 3b (route) ships behind `PULSE_DEEP_MARKET_EXPANSION_ENABLED=false` so we can compare engagement before/after enabling.

---

## OPEN: product questions for the user

These are decisions, not engineering details:

1. **OPEN:** Default ladder when an operator's `league_weights` are absent and we fall back to ours. The plan today is "Apuesta Total signals from Rogue are sufficient for v1, no manual ladder needed." Confirm.

2. **OPEN:** Per-fixture manual override surface. Operators can already promote fixtures via Rogue's own featured-events API (their normal merchandising flow). Should Pulse add its own per-fixture boost UI, or stay zero-config and let the operator manage importance through Rogue? Recommendation: zero-config for v1, defer Pulse-side admin to v2.

3. **OPEN:** Cost ceiling per "deep" fixture. Current $3/day total. A deep-mode fixture might burn $0.30-0.50 (more searches + more rewrites). With 4-5 deep fixtures per operator on a busy night, that's $1.50-2.50 of one day's budget. Options: (a) keep one global $3 budget, accept that high-value nights consume more; (b) carve out a per-fixture ceiling (e.g. `$0.40`) and let the rest of the engine spend the remainder.

4. **OPEN:** Calendar logic source-of-truth. Domestic season start/end + cup window dates per league. Static config, hand-maintained, low churn? Or pulled from an external sports calendar? Recommendation: static, baked into the `leagues` reference table.

5. **OPEN:** Per-operator score thresholds. Default `deep ≥ 0.7`, `minimal < 0.4`. Should an operator be able to override per-embed (some operators want a wider deep zone)?

6. **OPEN:** Audience signal cutoff. At what minimum click volume per league per operator do we let measured engagement override declared/inferred weights? Recommendation: 4+ weeks of data and minimum 1k clicks before any data-driven shift.

---

## Sequencing

| Phase | Scope | Effort | Status |
|---|---|---|---|
| 0 | Agree on this doc + answer the OPEN questions | 30 min product call | Doc agreed; OPEN questions still pending |
| 1 | PR 1 (capture signals) + PR 2 (`get_featured_events` + tag) | ~1 day agent work | **SHIPPED 2026-04-29 (PR #108) + calibrated 2026-04-30** |
| 2a | Score every fixture + log distribution; observability only | ~half day agent work | **SHIPPED 2026-05-01 (PR #109)** |
| 2b | Gradient routing — score → continuous multiplier on per-fixture knobs | ~half day agent work | **SHIPPED 2026-05-01 (PR #112)** |
| 3a | Market-depth observability — log per-group `MarketGroupOrder` distribution + admin endpoint | ~half day agent work | **In flight (this PR)** |
| 3b | Market-depth routing — singles + BB + combo deep-pool selection by `MarketGroupOrder` | ~1 day agent work + Kmianko regression | Not started; depends on 3a calibration data |
| 3c | Customer-facing offering — operator-visible breadth claim + UI badge | ~half day agent work | Not started |
| 4 | Calendar phase multiplier + derby table + knockout escalator (cheapest first slice = knockout) | ~half day agent work per slice | Not started |
| 5 | Audience signal feedback loop | Wave 4+ | Not started |
| 6 | Per-operator engine passes (when 5+ operators justify it) | Architectural | Not started |

**Phase 1 alone delivers most of the user-visible improvement** — UCL fixtures get tagged as featured, score high, deep mode runs more aggressive scouts. Most of the perceived "this is a huge game and we should have more" benefit lands here.

---

## Phase 3.5 — Narrative composer (PR #121, dark by default)

The breadth from Phase 3b is necessary but not sufficient. Without a way to read the news *as a story* and pick legs that orbit that story's subject, a broader market pool just produces more random combinations. Phase 3.5 is the bridge: subject + archetype + signal vocabulary + composer, all dark behind `PULSE_NARRATIVE_COMPOSER_ENABLED=false`.

**The model in one breath:** every news item resolves to a `NarrativeThesis` carrying *who/what the story is about* (`subject`), *what kind of story it is* (`archetype` — currently 10 starter shapes), and *what game-state signals it implies* (resolved per subject, e.g. `player.casemiro.discipline_pressure`). Markets in `market_meta.py` (50 entries) declare what signals they emit per direction. The composer scores combinations by signal overlap with the thesis, applies the archetype's `bet_shape_rule` as a hard filter (e.g. `discipline_only` rejects every Goalscorer leg for a `PLAYER_DISCIPLINE_RISK` story), and returns ranked combos.

Worked example: "Casemiro one yellow from suspension; Liverpool will target him" → archetype `PLAYER_DISCIPLINE_RISK` → subject Casemiro → signals include `player.casemiro.discipline_pressure`, `discipline.heavy.first_half`, `physicality.high` → composer returns BBs like `Casemiro To Be Booked + Cards 1H Over 2.5 + Total Match Fouls Over 24.5`. Not a goalscorer leg in sight. Today's INJURY/TEAM_NEWS theme would have produced *Casemiro Anytime Scorer + United to Win + BTTS* — wrong shape entirely.

### Self-learning hooks (built in, dormant for now)

| Hook | Captured today | Consumed by |
|---|---|---|
| Per-thesis confidence + alternatives | `narrative_telemetry.save_thesis()` | Future engagement model: *"did high-confidence matches drive clicks?"* |
| `is_uncertain` flag on low-confidence theses | `[narrative_uncertain]` log line | Manual review → extend `ARCHETYPES` keywords / add new archetypes |
| `llm_second_opinion_hook(news, rule_match)` | No-op today | Next PR: Haiku call when `confidence < 0.5` to overrule rule-based misses |
| Per-composition `legs_json + score + signal_overlap` | `narrative_telemetry.save_composition()` | Future: which `(archetype, market_type, leg_count, odds_bucket)` correlates with engagement |

### Files (PR #121)

- `app/engine/narrative_signals.py` — vocabulary (~30 signals + per-team/player/manager templates) + `resolve()` + `conflicts()`
- `app/engine/narrative_archetypes.py` — 10 starter archetypes (`PLAYER_DISCIPLINE_RISK`, `KEY_ATTACKER_OUT`, `TACTICAL_HIGH_PRESS`, `MANAGER_PRESSURE`, `PLAYER_FORM_STREAK`, `KEY_DEFENDER_OUT`, `RETURNING_PLAYER`, `TACTICAL_LOW_BLOCK`, `DERBY_INTENSITY`, `SET_PIECE_THREAT`) with keyword + hook matchers
- `app/engine/narrative_thesis.py` — `build_thesis(news)` returning subject + archetype + resolved signals + alternatives + uncertainty flag
- `app/engine/market_meta.py` — 50-market metadata catalogue with `emits_signals_by_direction` + `archetype_affinities` + `bet_shape_rule` constraints
- `app/engine/combination_composer.py` — `compose_candidates(thesis, market_pool)` → ranked `Combination` list, scored by signal overlap − conflict_penalty − orphan_legs
- `app/services/narrative_telemetry.py` — separate `narrative_theses` + `narrative_compositions` SQLite tables, no `CandidateCard` schema changes (sidesteps schema-drift risk)
- `tests/test_narrative_pipeline.py` — 28 tests including the booking-watch end-to-end

### What this PR is NOT doing

- Not flipping `PULSE_NARRATIVE_COMPOSER_ENABLED` on in production
- Not wiring the composer into `combo_builder` (that's PR #122 — alongside today's hand-coded themes for A/B comparison)
- Not calling Haiku (LLM second opinion hook is dormant)
- Not capturing engagement events (depends on AT live + click attribution)

---

## Cross-references

- `docs/production-mvp-plan.md` — production MVP context (locked 2026-04-28)
- `docs/operator-handoff-apuesta-total.md` — operator-facing iframe contract
- `docs/operator-adapter.md` — multi-tenant config drop (backlog)
- `~/.claude/projects/-Users-ianbradley-Product-mocks/memory/feedback_rogue_api_gotchas.md` — the IsTopLeague trap that informed signal selection
- `~/.claude/projects/-Users-ianbradley-Product-mocks/memory/feedback_match_infra_to_product_risk.md` — keep this design within MVP infra (no new services, no new infra)
- `~/.claude/skills/rogue-spec-lookup/SKILL.md` — for any further endpoint discovery
- `~/code/rogue-api-mcp/spec/openapi.json` — full Rogue API spec (post-2026-04 folder reorg; the prior `~/Downloads/rogue-api-mcp/` and `~/rogue-api-mcp/` paths are stale)

## Open links to update once decisions land

- The 6 OPEN questions above
- A `~/pulse-poc/docs/leagues-reference.md` for the hand-curated leagues table (Phase 2 dep)
- The `embeds` table migration once `jurisdiction` and `competition_floor` columns are added (Phase 2)
