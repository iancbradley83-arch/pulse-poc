# empty feed — stale catalogue (no periodic refresh)

- Started: ~2026-05-02 14:00 UTC (feed degrading; first card-decay)
- Detected: 2026-05-02 15:33 UTC (`[ops-bot] WARN — feed has 0 cards (<5)`)
- Closed: 2026-05-03 10:43 UTC (post PR #113 first cycle, 19 cards live)
- Duration: ~20 hours visible-empty, ~18 hours from detection to fix
- Severity: P2 — public widget showed 0 cards; no Anthropic spend, no data loss; no operator-side breakage (Apuesta Total integration still in setup)

## Summary

A two-day-old Railway deployment slowly slipped into a state where every fixture in its boot-loaded catalogue had a kickoff in the past. The HOT-tier classifier reported `total=10 -> eligible=0 -> top=0` every cycle. The engine bypassed every tier loop. Cards already on the feed expired naturally over ~24h via the 6h TTL sweep. Net effect: feed at 0 cards from ~2026-05-02 15:00 UTC until the next redeploy.

The detection signal fired correctly (`[ops-bot] WARN — feed has 0 cards`), but no phone-only fix existed:
- `/rerun` ran successfully but did nothing useful (stale catalogue → engine still bypassed)
- `/resume` failed with `Not Authorized` due to a malformed `variableUpsert` mutation in the ops-bot code (passing the environment **name** "production" instead of the environment UUID). The Railway API token itself is fine — verified post-incident with the same token doing successful reads + writes once the env UUID was used. **The 30 Apr token rotation was clean. The bot has a query-format bug, not a credentials problem.**

## Timeline

- **2026-05-01 14:02 UTC** — Phase 2b deploy (PR #112) goes live. Boot loads 10 fixtures from Rogue. Engine runs normal cycles, ~$2.00 spend, 27 cards in feed by EOD.
- **2026-05-01 18:31 UTC** — User runs `/resume` on Telegram bot. Both `PULSE_RERUN_ENABLED` and `PULSE_NEWS_INGEST_ENABLED` `variableUpsert` calls fail with `GraphQL errors: 'Not Authorized'`. **Both env vars were already `true` in Railway** (post-incident verification: read all 47 production env vars with the same token; both flags fine). The /resume call was a no-op against already-true state, but the bot's mutation is malformed — passes `environmentId: "production"` (the name) instead of the UUID. Railway returns generic `Not Authorized` for shape errors. Bug is in the bot, not the token. (Pre-existing — not caused by this incident; surfaced by it.)
- **2026-05-02 ~00:00 UTC** — Evening digest: `Cost: $1.77/$3 (59%) — 67 calls, 27 cards in feed`. Healthy.
- **2026-05-02 ~03:00 UTC onwards** — Catalogue's fixtures begin slipping into the past. Tier loops still run every cycle, see 0 in-window fixtures, log `all 0 fixtures skipped_fresh — engine bypassed`. No new cards. TTL sweep starts removing existing cards as their 6h windows expire.
- **2026-05-02 11:00 UTC** — Morning digest: `19 cards in feed`. Decay visible but not yet alerting.
- **2026-05-02 15:33 UTC** — `[ops-bot] WARN — feed has 0 cards (<5)`. Push alert fires correctly with `[FEED] [RERUN] [DISMISS]` buttons.
- **2026-05-02 16:49 UTC** — User taps `[RERUN]` → `confirm rerun Pulse engine?` → confirms. Bot reports `rerun: done — engine will run on next cycle`. **Effect: nothing.** The rerun fans out across tier loops but every tier still sees 0 in-window fixtures. Cycle log: `scouted=0 candidates=0 cost_actual=$0.0000`. (User does not have visibility into this from phone — the bot just confirms the trigger.)
- **2026-05-03 02:03 UTC** — Second `[ops-bot] WARN — feed has 0 cards` fires. User has no further phone-only actions to try.
- **2026-05-03 10:11 UTC** — PR #113 (periodic catalogue refresh) merged + auto-deployed. Boot reloads catalogue with today's fixtures.
- **2026-05-03 10:42 UTC** — First HOT-tier cycle post-defer. `total=10 -> eligible=8 -> top=5`. 19 cards published. Feed populated. Daily cost $0.79.
- **2026-05-03 11:02 UTC** — Verified: 19 cards live, gradient routing emitting per-fixture knobs, catalogue-refresh loop active.

## Root cause

The catalogue is loaded once in `_load_rogue_prematch()` at boot and never refreshed. Tier loops *consume* the catalogue but don't refresh it. As deployments age past ~24h, every kickoff loaded at boot drifts into the past, the HOT-tier `kickoff_window=90m..6h` filter rejects all of them, and the engine bypasses every cycle.

This is a one-line architectural omission — the engine had a "loader" but no "refresher". Latent for the entire life of the project; surfaced here because Railway hadn't redeployed for two days.

## Contributing factors (each made the incident worse)

1. **Phone-only ops gap.** The bot's `/rerun` is only useful when the catalogue is fresh. Triggering it against a stale catalogue runs zero-cost zero-card cycles. The bot reported success ("rerun: done") with no signal that nothing happened. User had no way to tell from phone why the rerun didn't fix the feed.
2. **Bot query bug masquerading as token failure.** The bot's `/resume`, `/pause`, `/flag` implementations send `variableUpsert` with `environmentId: "production"` (name string) instead of the environment UUID. Railway responds with generic `Not Authorized`. This looked like a token-permission issue; it isn't. The 30 Apr rotated token has full project read+write — verified post-incident by upserting `PULSE_RERUN_ENABLED=true` (idempotent) successfully via the same token. **The bot's main "fix it from phone" muscle has been broken since at least 01 May, but for a code bug not a credentials reason.** Fix is a few-line change in `ops-bot/` to look up the env UUID before the mutation.
3. **No catalogue-age signal in alerts or digests.** Digests show `Cards in feed: N` but not "catalogue last refreshed X hours ago". The `feed has 0 cards (<5)` warning didn't say *why* — could have been pause, cost-tripwire, or staleness, but only one of those is fixable from phone.
4. **No automatic recovery.** Pulse has cost tripwires that auto-pause and feed-rehydrate that loads on boot, but no "feed has been empty for 2 cycles, force a catalogue refresh" guard.
5. **No `/redeploy` attempt during incident.** The user did not try `/redeploy` from the bot during the 18h window. (`/redeploy` is the operation that *would* have unstuck this — fresh boot = fresh catalogue load.) Whether it would have worked depends on whether the broken token had the `serviceInstanceRedeploy` permission; this is untested.

## What worked

- **WidgetAlerter / feed-cards alert fired correctly** within ~30 min of the feed dropping below 5 cards (02 May 15:33 UTC). The detection layer is fine.
- **Catalogue refresh PR (#113) was a 90-min fix once on a laptop.** Single function added, 6 unit tests, clean rebase, single squash-merge, single redeploy. Engine cycle one tier-defer later (10:42 UTC) was healthy with no manual intervention.
- **No data loss, no Anthropic spend during the incident** (engine bypassed every cycle = $0). Daily cost on 02 May was $0.21 vs the usual $1-2 — actually saved money during the outage.

## Resolution

PR #113 (`fix(catalogue): periodic refresh`) shipped a `_catalogue_refresh_loop` task spawned alongside the tier loops. Every `PULSE_CATALOGUE_REFRESH_SECONDS` (default 14400 = 4h) it calls `fetch_soccer_snapshot` and atomically swaps `simulator._games` + the market catalog. Catalogue-only — no LLM. Floor 900s, kill switch `PULSE_CATALOGUE_REFRESH_ENABLED`, fail-soft on Rogue errors (keep prior snapshot). Verified live with `[PULSE] Catalogue refresh active — every 14400s` at boot.

## Action items

### Closed by this incident
- ✅ **PR #113** — periodic catalogue refresh, shipped 2026-05-03 10:11 UTC.
- ✅ **Phase 2b verified end-to-end** — `[gradient] enabled —` and per-fixture routing log lines emitted on the 10:42 UTC cycle, gradient routing functioning as designed.
- ✅ **Incident postmortem + phone playbook entry** — this file + new scenario in `docs/PLAYBOOK.md`.

### Open (deferred — track in next-session followups)
- ⏳ **Fix the ops-bot `variableUpsert` query.** Bot needs to look up the environment UUID once at boot (or cache it from `project.environments`) and pass that to `variableUpsert` instead of the name "production". A few lines in `ops-bot/` — unblocks `/resume`, `/pause`, `/flag` end-to-end. Token rotation is **not** required and would not help.
- ⏳ **Add catalogue-age signal to digests + alerts.** Surface `catalogue_age_h` in morning/evening digests and in the `feed has 0 cards` warning so the symptom-vs-cause ambiguity is gone from phone.
- ⏳ **Add `/refresh-catalogue` bot command** wrapping a new `POST /admin/catalogue-refresh` endpoint. Provides a phone-only fix path for stale-catalogue cases that's lighter than `/redeploy` (no rebuild, just one Rogue fetch). Useful even now that the loop runs every 4h — the bot can force an early refresh after an inferred-stale alert.
- ⏳ **Test `/redeploy` end-to-end** once the bot's variableUpsert is fixed (deployment-trigger mutation likely has the same risk of using `production` instead of UUID). This incident would have been the perfect proving test if the user had tried it.
- ⏳ **Self-healing: add a "feed empty for N consecutive cycles → force catalogue refresh"** guard inside the tier loop. Even with the periodic refresh shipped, a Rogue outage during a refresh window could re-create this state. Cheap belt-and-braces.

## Lessons

- **A scheduled background task that loads state on boot but never refreshes it is a latent timer.** Audit other "load once at boot" surfaces in pulse-poc (e.g. `embeds` config, `MarketCatalog` whitelist hot-loading, league reference data) for the same pattern.
- **Generic auth errors hide bug shapes.** Railway returns `Not Authorized` for both genuine permission failures **and** input-shape errors (wrong env identifier format). Diagnostic queries should include a self-test that does an idempotent write to a known-safe variable so "token works" is verified end-to-end, not just on reads. Same goes for the bot — its boot probe should send a real `variableUpsert` round-trip, not just a `__typename` ping.
- **My investigation tools had the same bug as the production bot.** The "Not Authorized on variables/me" finding I cited for an hour came from passing `environmentId: "production"` instead of the UUID. The **wrong claim survived in the incident draft until I tested the mutation directly.** Lesson: when you assert a credential is broken, prove it by exercising the smallest possible successful operation, not by interpreting error messages.
- **Symptom alerts need cause hints.** `[ops-bot] WARN — feed has 0 cards` is necessary but not sufficient on phone-only ops. The same symptom has multiple root causes with different fixes.
