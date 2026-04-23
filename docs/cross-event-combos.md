# Cross-Event Storyline Combos — Design

**Status:** spike (branch `feat/cross-event-storyline-combos-spike`).
**Replaces:** ROADMAP Stage 3d ("Goalscorers of the Day" — generic
anytime-scorer accumulator with odds-band filter). That scope was
stats-first and violated the Pulse north star ("news is the trigger, not
stats" — `project_pulse_principles.md` #1). This version is narrative-first.

## Core insight

> "We also aren't making many combo bets across games. We need to find
> stories across multiple games — relegation battle, chase for Europe etc.
> Same for goalscorers, Golden Boot chase." — user, 2026-04-23

For cross-event combos, **the narrative is synthesized across fixtures** —
it is NOT rewritten from a single news item. The engine recognises a pattern
across fixtures, then freshly authors a storyline on top. Example: "Three
clubs fighting to survive this weekend" spans three fixtures whose combo
legs are (each bottom-of-table side's opponent to win); the headline is
authored from the pattern, not any one news item.

This is a fundamentally different shape to the single-fixture pipeline,
where one news item drives one card.

## Storyline types (v1 scope)

| # | Storyline | Data requirement | Spike? |
|---|---|---|---|
| 1 | **Relegation 6-pointers** — bottom-N clubs all playing same matchweek. Combo = their opponents to win. | League standings. Rogue has no league-table endpoint; LLM web search required. | TODO |
| 2 | **Europe chase** — teams fighting 4th/5th/Europa spot all playing same MW. Combo = each to win or +1.5 AH. | League standings; "who is in 4th-7th by league". LLM web search. | TODO |
| 3 | **Golden Boot race** — top scorer + 2-3 chasers all playing same weekend. Combo = anytime-scorer for all of them. | Per-league top-scorer table. Rogue has no top-scorer endpoint; LLM web search. | **YES (spike)** |
| 4 | **Manager-under-pressure weekend** — 2-3 managers one loss from sack, all playing. Combo = their teams' opponents or under goals. | Editorial signal. LLM web search only. | TODO |
| 5 | **Debut/return weekend** — newly-signed or returning star(s) making first appearance. Combo = each to score or assist. | Transfer/injury news aggregated across fixtures. Reuses per-fixture `NewsItem.hook_type=TRANSFER`. | TODO |

### Why Golden Boot for the spike

- **Cheapest data path.** LLM web search ("who is in the top 5 of the EPL
  scoring chart as of April 2026, listed with current goal count") returns
  3-5 named players deterministically; no league-table scraping required.
- **Clean market mapping.** Each fixture in Rogue's upcoming catalogue has
  an anytime-goalscorer market if it is a top-league fixture with the
  `includeMarkets=all` catalogue load (PR #17). We already have
  `_match_player_selection` in `candidate_builder.py` that takes a list of
  mention names and returns the matching selection — we reuse it.
- **Narrative is obvious and the audience (engaged casual) already tracks
  it.** "Haaland, Watkins and Isak all playing this weekend — five goals
  between them and the Golden Boot still alive" reads like exactly the
  kind of connective tissue Pulse should provide.

Relegation + Europe chase are deferred because they need standings data,
which needs a league-table strategy (see Open questions).

## Pipeline shape

```
Existing single-event path (untouched):
  NewsIngester ── per fixture ──▶ NewsEntityResolver ──▶ CandidateBuilder ──▶
    CandidateCard(single/BB) ──▶ store

New cross-event path (additive, runs ONCE per engine cycle):
  StorylineDetector ──▶ StorylineItem (type + participant names + fixtures)
         │
         ▼
  CrossEventBuilder  (pick one market leg per participating fixture)
         │
         ▼
  ComboValidator     (calculate_bets, reads Bets[Type='Combo'].TrueOdds)
         │
         ▼
  CombinedNarrativeAuthor (Sonnet — fresh headline + angle synthesising
                           the storyline across fixtures)
         │
         ▼
  CandidateCard(bet_type=COMBO, selection_ids=[leg1, leg2, leg3])
         │
         ▼
  store (same SQLite table; combo pricing already wired in main.py)
```

### What is reused vs new

Reused:
- `NewsIngester.ingest_for_fixture` — still runs per-fixture for single-event
  cards; storyline path calls a separate LLM prompt instead.
- `_match_player_selection` in `candidate_builder.py` — maps a player name
  to a goalscorer selection for a given market.
- `RogueClient.calculate_bets` — combo pricing. No changes needed.
- `CandidateCard` — `bet_type=COMBO` + populated `selection_ids`. The combo
  pricing loop in `_run_candidate_engine` in `main.py` already stamps real
  `Bets[Type='Combo'].TrueOdds` onto any `COMBO` candidate that hasn't been
  priced yet.
- `CandidateStore` — no schema drift in v1 (we encode storyline metadata in
  existing fields — `narrative` holds synthesised copy, `hook_type=OTHER`,
  `news_item_id` NULL).

New (this spike):
- `backend/app/services/storyline_detector.py` — Sonnet call that lists
  active storylines of a given type across the catalogue.
- `backend/app/engine/cross_event_builder.py` — turns a `StorylineItem` +
  its fixtures into a `CandidateCard` with populated `selection_ids`.
- `backend/app/engine/combined_narrative_author.py` — Sonnet call that
  writes the cross-event headline + angle from the pattern (not from a
  single news item).
- `backend/app/models/news.py` — `StorylineItem` schema (pydantic, NOT
  persisted in v1 — lives in memory for the duration of a cycle).

## Narrative authoring

- Input: storyline type ("golden_boot"), participants (list of
  `{player, team, goals, fixture_id, market_leg_odds}`), combined odds
  (real, from calculate_bets).
- Output: `{headline, angle}` — 6-10 word headline naming the shared story;
  ≤25 word angle tying the legs together.
- Model: Claude Sonnet 4.6 (same as `NarrativeRewriter`). Volume is low
  (1-5 cross-event combos per cycle) so quality > cost.
- Voice: same `VOICE_BRIEF` as singles — angle not pick, active voice, no
  wire-service tics. System prompt is cross-event-specific (written
  fresh, not a repurposed rewriter call) because the input shape is
  different (N fixtures, not 1).

## Odds/volume targets

- Legs per combo: 3-5. Three is the minimum for a "story across games" to
  feel coherent; five is the ceiling before correlation math gets noisy.
- Odds range: 5-30 decimal. Below 5 isn't interesting enough to frame as a
  weekend story; above 30 stops being engaged-casual and becomes long-shot.
- Per-cycle cap: 3 storyline combos per rerun. Env knob:
  `PULSE_STORYLINE_COMBOS_MAX` (default 3).

## Card shape on the feed

Today's feed renders:
- Single: `market` + `market.selections` (or trimmed to matched player).
- BB: `legs[]` + `total_odds` + `virtual_selection` — the stacked-pick block
  in `static/app.js`.

Cross-event combo:
- Reuse the BB multi-leg card shape. The existing `legs[]` rendering
  already shows `market_label + label + odds` per leg — that works for a
  Relegation 6-pointers combo showing "Everton @ Arsenal · Arsenal win
  @ 1.40" three times over.
- `bet_type = "combo"` on the Card (not "bet_builder"). Frontend should
  label the card "CROSS-EVENT COMBO" or similar in the badge. **This is
  the one frontend change** — a string change in the card badge logic.
  `virtual_selection` stays null for combos.
- Storyline header: the synthesised headline IS the card headline. No new
  card field required.

## Pricing

- `calculate_bets(selection_ids)` returns a `Bets` array; for a cross-event
  combo we read `Bets[Type='Combo'].TrueOdds`. The operator's combo bonus
  is already baked in.
- This path is already wired in `_run_candidate_engine` in `main.py`
  (lines 764-799): any candidate with `bet_type=COMBO` and unset
  `price_source` gets priced in a single post-engine sweep. No changes
  required — `CrossEventBuilder` just emits COMBO candidates with
  populated `selection_ids`.

## Open questions

1. **Standings data strategy** — Relegation + Europe chase both need a
   current league table. Options: (a) LLM web search per league per cycle,
   (b) cached Wikipedia/FBRef scrape, (c) add a league-table endpoint to
   `RogueClient` if it exists (not in the spec). **Decision needed: is it
   acceptable to have the LLM fetch standings with a web search each
   cycle?** Cost is ~$0.02 per league; we cover 6 leagues ≈ $0.12/cycle.
2. **How many storylines per cycle is too many?** v1 caps at 3 total
   across all storyline types. If a matchweek has both a Golden Boot race
   AND a relegation battle, do we show both, or pick the more compelling
   one? Recommend: both, up to the cap.
3. **Dedupe with single-event cards.** If a Golden Boot combo surfaces
   "Haaland scores", and the same cycle has a single "Haaland fit to play"
   card, do we drop one? Probably keep both — they tell different stories.
   Confirm.
4. **Matchweek window.** "Same weekend" is ambiguous. v1 uses "fixtures
   within the next 96h from engine run". Good enough? Should it be
   per-league matchweek (harder — requires league table)?
5. **Storyline authorship — can the LLM hallucinate a Golden Boot leader?**
   v1 has the detector include `goals` per player so we can sanity-check
   (if top scorer has <10 goals by April we know something is off). Long
   term: standings/top-scorers need a deterministic source.
6. **Persisting `StorylineItem`s for the admin page.** v1 doesn't — they
   live in memory for one cycle. If we want to label-and-learn on them
   (like `candidate_reviews`), they need a table. Defer to post-spike.
7. **Frontend badge copy.** "Cross-event combo" is accurate but clunky.
   "Weekend Storyline"? "Across the Weekend"? User to decide.

## TODO (post-spike, not in this PR)

- Relegation 6-pointers storyline type (needs standings).
- Europe chase storyline type (needs standings).
- Manager-under-pressure storyline type (editorial signal — doable via LLM
  web search, but verdict fragility is real).
- Debut/return storyline type (requires aggregating per-fixture
  `TRANSFER` and `INJURY-return` news across the engine run).
- Persist `StorylineItem` in SQLite with its own table for learning-loop
  labels.
- Frontend: distinct badge + subtle "3 games · one story" micro-caption
  above the legs.
