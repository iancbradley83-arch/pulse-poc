# Pulse operator playbook

Phone-first, single-founder operational guide. For each scenario: what triggers it, what the bot is telling you, the lowest-risk first move, when to escalate, and how to feed it back into the system.

The companion `RUNBOOK.md` is engineering reference (how to verify a deploy, pull logs, tune rate limits). This file is decision support: "X happened — what do I do?"

Bot access: `/playbook` lists scenarios; `/playbook <topic>` returns one section. Use `/runbook <topic>` for engineering details a scenario references.

---

## Coverage matrix

What can be handled phone-only vs needs a laptop. ✓ = fixable from Telegram, △ = partial (visible but not actionable), ✗ = laptop required.

| Scenario | Detection | Triage | Fix |
|---|---|---|---|
| Cost ladder alert ($1 / $2 / $2.95) | ✓ push | ✓ `/breakdown` | ✓ `/pause` `/snooze` |
| Pulse `/health` 5xx >2min | ✓ push | ✓ `/status` `/logs` | ✓ `/redeploy` |
| Widget HTML broken (200 OK, won't render) | ✓ push (WidgetAlerter, 5min poll) | ✓ `/preview` `/status` | ✓ `/redeploy` (transient); ✗ template/code bug needs laptop |
| Deploy FAILED / CRASHED | ✓ push | ✓ `/logs` | ✓ `/redeploy` |
| Feed unhealthy (<5 cards or hook collapse) | ✓ push (smart-button: REDEPLOY when stale, RERUN when fresh) | ✓ `/feed` `/breakdown` | ✓ `/redeploy` (stale catalogue) or `/rerun` (fresh) |
| Deep-link 3/5 failing | ✓ push | ✓ `/preview` | △ `/pause` (halt new bad cards); fix is laptop |
| Engine paused and forgotten | △ digest + manual `/status` | ✓ `/status` | ✓ `/resume` |
| Bad card visible to users | △ operator report or eyeballing `/cards` | ✓ `/card <id>` | ✗ `/blacklist` blocked on Pulse PR |
| New Sentry exception class | ✗ webhook not yet wired | — | — |
| Bot itself dies overnight | ✗ UptimeRobot not wired | — | — |
| Operator reports widget broken | ✗ webhook not yet wired | — | — |
| SQLite corrupted / data loss | ✗ no detection | — | ✗ no `/restore` (R2 backup not wired) |
| Anthropic / Rogue API down | △ engine errors in `/logs` | △ `/logs` | △ wait it out; `/pause` if cost spikes |
| Railway platform outage | ✗ both bot + Pulse degraded | — | ✗ wait |

If a row says ✗, that gap is on the roadmap (`docs/pulse-session-2026-04-29-followups.md`) or pending manual setup.

---

## Support workflow on Telegram — first 90 seconds

The bot pushes alerts proactively; this section is the general triage ritual that applies before you dive into a specific scenario. The aim: from alert-fires to "fix-or-snooze" decision in under 90 seconds.

### The five-step ritual

1. **Read the alert text in full.** Every push includes the symptom (what), an inferred cause hint when the bot has data for it (e.g. `catalogue 39h old — likely stale`), and 2-3 commands you can reach for. Don't skip the second line.
2. **Tap the highlighted action button** (the one that's not `[DISMISS]`). The bot picks the right primary action based on what it knows about state. For empty-feed alerts: `[REDEPLOY]` when catalogue is stale, `[RERUN]` when fresh — the bot has already inferred which. For cost alerts: `[PAUSE]`. For health: `[STATUS]` then `[REDEPLOY]`.
3. **Confirm the action** if the bot prompts. Most write actions show `confirm: <action>?` with a `[confirm]` button or "type yes" within 30s. This is intentional — accidental taps shouldn't pause the engine or trigger redeploys.
4. **Wait the time the bot tells you, then verify.** Each action has an expected recovery time:
   - `/redeploy` → 4 minutes for cold start. Don't act again before that.
   - `/rerun` → 60-90 seconds for the next tier cycle. Verify with `/feed`.
   - `/pause` → instant; verify with `/env PULSE_RERUN_ENABLED`.
   - `/resume` → instant; verify with `/env PULSE_RERUN_ENABLED` showing `true`.
5. **If still broken**, open an incident: `/incident start "<short title>"`. Then escalate to laptop. Don't keep tapping action buttons in a loop.

### When to dismiss vs snooze vs act

- `[DISMISS]` clears the alert from the chat without doing anything. Use when you've confirmed the symptom is benign (e.g. cost alert during a known boot-scout burst, or a feed dip that recovered before you got to it).
- `/snooze <kind> <duration>` suppresses re-firing of the same alert class for the duration. Kinds: `cost`, `deploy`, `health`, `feed`, `deeplink`, `all`. Use when you're choosing to live with a known symptom (e.g. `/snooze feed 1h` while a Rogue outage clears).
- Act when the bot's primary button is the right thing AND the inferred cause matches what the alert text says. If they disagree, drop into `/status` + `/breakdown` first to gather context before acting.

### Read-only commands you should know cold

These don't change state — safe to run any time:

- `/status` — health + cost + deploy + cards-in-feed + key env vars in one screen
- `/breakdown` — today's spend bucketed by kind (`scout`, `rewrite`, `narrative`, `boot`, `verify`)
- `/feed` — current public feed audit (counts, hook mix, missing prices)
- `/cards [page]` — paginated card list; `/card <id>` for one card's detail
- `/logs [n]` — last n WARN/ERROR lines from the latest deployment
- `/env <KEY>` — current value of any `PULSE_*` env var on Railway
- `/preview` — render a synthetic `/api/feed` snapshot through the public widget URL + run deep-link HEAD checks
- `/runbook <topic>` — engineering reference for a topic (e.g. `/runbook cost`, `/runbook redeploy`)
- `/playbook <scenario>` — this file's section for a scenario

### Write commands that confirm before acting

These DO change state. Every one shows a 30-second confirm prompt before executing:

- `/pause` — set `PULSE_RERUN_ENABLED=false` AND `PULSE_NEWS_INGEST_ENABLED=false` on Pulse Railway
- `/resume` — set both back to `true`
- `/rerun` — POST `/admin/rerun` to trigger a candidate engine cycle (no redeploy)
- `/redeploy` — trigger Railway `deploymentRedeploy` on the latest deployment (forces fresh boot, ~4 min cold start, reloads catalogue)
- `/flag <NAME> <true|false>` — set any other `PULSE_*` env var; useful for kill-switching specific features without a redeploy
- `/snooze <kind> <duration>` — see above

### Incident rituals

- **Open** when you start triaging anything non-trivial: `/incident start "<title>"`. Doesn't need to be perfect — the title is just for grouping notes.
- **Note as you go**: `/incident note "<text>"`. Capture the symptom timestamp, what the bot showed, what action you took, what came back. Future-you reading this incident in `pulse-poc/incidents/` will thank present-you for the context.
- **Close** when the symptom is gone: `/incident close`. Bot commits the postmortem to `pulse-poc/incidents/YYYY-MM-DD-<slug>.md` via the GitHub API. You can flesh out the Resolution + Lessons sections later from a laptop.

### When to escalate to laptop

- Two consecutive primary-button actions don't recover (`/redeploy` then `/redeploy` again with no improvement → log dive needed).
- The bot reports a write action failed AND retry-once didn't help.
- You see the same alert recur on the next day's digest after a phone-fix that "worked" — usually means the fix was symptomatic, not causal.
- A symptom is novel (no playbook scenario matches the alert text). Triage on phone, escalate to laptop for actual fix.

### What the bot does NOT do

- Bot does **not** auto-execute write actions on alerts. Every push is informational + an action button you must tap. Even `/snooze` requires you typing/tapping it.
- Bot does **not** read or send credentials. All secrets live on Railway env vars or in macOS keychain on Ian's laptop. The bot uses its Railway env vars to authenticate to Railway and Pulse APIs.
- Bot does **not** make code changes. `/flag NAME true|false` flips Railway env vars (which trigger redeploys), but never touches git or merges PRs.
- Bot does **not** know about non-Pulse things. The polybolts twitter bot, rogue-api-mcp work, and any other Ian project is invisible to it.

---

## Scenario: cost ladder alert

Symptom:
- Bot pushed `[ops-bot] CRITICAL — cost crossed $X today` with `[PAUSE] [BREAKDOWN] [DISMISS]` buttons.

What it means:
- Today's LLM spend crossed a threshold ($1, $2, or $2.95). Engine self-kills at $3.

First move (90 seconds):
1. Tap `[BREAKDOWN]` (or send `/breakdown`).
2. Look at `By kind:` — which bucket is dominant?
   - **`news_scout` high after a redeploy** → boot churn, likely benign. Watch one cycle.
   - **`rewrite` high** → cache miss; engine may have stale narrative cache. Flag for laptop review.
   - **`narrative_generator` high** → too many fresh candidates; may need a tighter filter.
3. Look at `Cards: N in feed`. If `$/card in feed` is healthy (<$0.10) and the spend just spiked from a redeploy, it's noise.

Decide:
- **Productive burn** (cards being generated, $/card reasonable) — `/snooze cost 1h` and watch.
- **Unproductive burn** (no new cards, climbing) — tap `[PAUSE]` and confirm. Pulse self-kills at $3 anyway, but don't ride it down.

Escalate to laptop if:
- After `/pause`, daily total still climbing on next poll → worker process not honouring kill switch (engine bug).
- Same kind keeps spiking across multiple days → cost-tracking telemetry is off, or there's a pricing model bug.

Learning step:
- Open an incident: `/incident start "cost spike YYYY-MM-DD"`.
- Note the bucket + spend at trigger: `/incident note "scout=$0.57 rewrite=$0 narrative=$0.10 boot=$2.11 — burnt at HH:MM"`.
- Close after triage; the postmortem commits to `pulse-poc/incidents/`.

---

## Scenario: Pulse `/health` 5xx >2 minutes

Symptom:
- Bot pushed `[ops-bot] CRITICAL — Pulse /health failing for ~Nm` with `[STATUS] [REDEPLOY] [DISMISS]` buttons.

What it means:
- The Pulse FastAPI process is unreachable or returning 5xx for two consecutive 60-second polls.

First move:
1. Tap `[STATUS]` — confirm what the bot sees end-to-end (cost, deploy, feed, engine vars).
2. Send `/logs 30`. Look for: `Traceback`, `Application startup complete`, `OOMKilled`, `502 Bad Gateway`.

Common causes & response:
- **Cold start in progress** (you just merged something) — wait. Pulse cold start is ~4 min per `RUNBOOK.md`. Do not redeploy.
- **Crashed post-boot** (no `Application startup complete` in logs after boot) — tap `[REDEPLOY]`. If the redeploy fails too, look at build logs (laptop).
- **OOMKilled** in logs — Railway memory exhausted. Increase service memory in Railway UI, then `/redeploy`. Out of phone scope; laptop.
- **Edge 502 but Railway shows SUCCESS** — Railway routing flake. Wait 60s and re-poll `/status`.

Escalate to laptop if:
- `/redeploy` succeeds but `/health` still failing 5 min later → underlying app bug, not infra.
- Logs show repeated `ModuleNotFoundError` → bad deps in `backend/requirements.txt`.

Learning step:
- `/incident start "pulse health outage"`; `/incident note` with the cause line from logs; `/incident close`.

---

## Scenario: deploy FAILED / CRASHED

Symptom:
- `[ops-bot] CRITICAL — pulse-poc deploy <commit> FAILED` with `[REDEPLOY] [LOGS] [DISMISS]`.

What it means:
- Railway tried to build or run the latest commit and the container exited.

First move:
1. Tap `[LOGS]` (or `/logs 50`).
2. Grep mentally for: `error`, `Traceback`, `ModuleNotFoundError`, `permission denied`.

Common causes:
- **Build failed at `pip install`** → bad `backend/requirements.txt` line; revert the offending commit on laptop.
- **Build SUCCESS but `Application startup` never appeared** → app crashes on boot; usually env-var typo or an import bug.
- **`COPY backend/...` not found** → `railway.toml` or `Dockerfile` path drift (we hit this on 2026-04-22 and 2026-04-28).

Phone fix:
- If the failed deploy is for code you control and the previous commit was healthy — tap `[REDEPLOY]` first to retry (sometimes a transient Railway build flake). If still red:
- `/runbook rollback` for the rollback procedure.
- Open the laptop only when you actually need to revert a commit or change code.

Escalate to laptop if:
- Two consecutive deploys fail.
- Build log error is unfamiliar.

Learning step:
- `/incident start "deploy failure"`; capture the failing commit SHA + exact error line.

---

## Scenario: feed unhealthy

Symptom:
- `[ops-bot] WARN — feed has N cards (<5)` with a smart-button keyboard:
  - **`[FEED] [REDEPLOY] [DISMISS]`** when the bot inferred a stale catalogue (alert text includes `catalogue last refreshed Xh ago — likely stale`).
  - **`[FEED] [RERUN] [DISMISS]`** when the catalogue is fresh (alert text includes `catalogue Xm old (fresh)`).
  - Falls back to `[FEED] [RERUN] [DISMISS]` with no age hint when older Pulse versions don't expose catalogue age.
- Or `> 80% same hook_type` (hook-collapse) with `[FEED] [RERUN] [DISMISS]`.

What it means:
- Either content drained out (kickoffs passed, no fresh fixtures scouted), the engine is stuck on one hook type, OR the catalogue is stale (long-running deploy, all kickoffs in the past — see incident `incidents/2026-05-03-empty-feed-stale-catalogue.md`). The bot's primary button reflects which.

First move (90 seconds):
1. Tap `[FEED]` — see the hook + league mix + missing-prices counts.
2. `/breakdown` — confirm engine cycles ran today and produced candidates.
3. `/env PULSE_RERUN_ENABLED` — make sure engine isn't paused.
4. `/status` — note `Deploy:` age. **If the deploy is older than ~24h, suspect a stale catalogue first.**

Common causes & phone fix:

| Cause | Signal | Phone fix |
|---|---|---|
| Engine paused | `/env` shows `PULSE_RERUN_ENABLED=false` | `/resume`. (Currently broken — see ⚠️ below.) |
| News cache stale, fresh fixtures available | `/breakdown` shows recent cycles produced 0 candidates but eligible > 0 | `/rerun`, recheck `/feed` after 60–90s |
| **Stale catalogue** (deploy >24h old, all kickoffs in past) | `/breakdown` shows `eligible=0` every cycle; `/status` shows old deploy | `/redeploy` — fresh boot reloads today's catalogue. Periodic refresh runs every 4h since 2026-05-03 (PR #113), so this is rarer now. |
| Catalogue cap reached & most post-kickoff | `/feed` empty but `/breakdown` shows engine ran | Wait for next catalogue refresh (≤4h). No phone fix. |
| Cost tripwire fired | `/breakdown` shows daily ≥ $3 | `/snooze cost 6h` won't help — laptop work. |

Why `/rerun` might not unstick this:
- `/rerun` triggers tier-loop fan-out, but each tier still filters fixtures by kickoff window. If the catalogue is stale (every fixture in the past), the tiers all see `eligible=0` and the engine bypasses every cycle. The bot will report `rerun: done — engine will run on next cycle`, but the next cycle does nothing. **Always verify with `/feed` 60-90s after `/rerun`** — silence-then-success pattern means real recovery; silence-then-still-empty means staleness or another root cause.

Escalate to laptop if:
- After `/redeploy`, `[catalogue-refresh] swapped — fixtures=0` repeats across two cycles (Rogue-side issue, our /v1/multilanguage/events query, or a regional outage).
- Repeated `/rerun` produces 0 candidates over 2 cycles WITH eligible > 0 (engine bug or quality gate stuck).
- Hook diversity stays collapsed across days (storyline mix needs tuning).

Learning step:
- Open an incident: `/incident start "feed empty YYYY-MM-DD"`.
- Capture: deploy age at trigger, eligible count from cycle log, whether `/rerun` or `/redeploy` recovered. Close after.

> 💡 **If a write action returns `Not Authorized`, retry once before assuming it's broken.** Railway's API surfaces transient internal-server errors with the same wording as actual permission failures. Verified post-incident: token + env UUID + bot mutation all work end-to-end; the 01 May `/resume` failure was a one-off. Don't rotate tokens chasing this — it won't help. (Roadmap: bot will gain a one-time auto-retry on write actions.)

---

## Scenario: deep-link 3-of-5 failing

Symptom:
- `[ops-bot] WARN — deep_links failing for 3/5 sampled cards` with `[PREVIEW] [PAUSE] [DISMISS]`.

What it means:
- HEAD requests to the operator's slip URL (Apuesta Total) are returning non-2xx for the majority of sampled cards. Either the operator changed their URL pattern, the bscode mint is producing invalid codes, or the operator is down.

First move:
1. Tap `[PREVIEW]` (or `/preview`) — see the actual deep-link URLs and their HEAD statuses.
2. Tap one URL on your phone — does the operator slip load with selections?

Phone-only triage:
- **All return 404** → operator URL pattern changed. Halt content production: tap `[PAUSE]`, then DM the operator. Laptop work to update the adapter.
- **All return 5xx** → operator backend down. Wait or DM operator. Don't pause Pulse — content is fine, it's the destination.
- **Mixed** → individual bscode mint failure. Check `/logs` for `KmiankoSlipMinter` errors.

Escalate to laptop:
- URL pattern change requires editing `backend/app/services/kmianko_slip_minter.py`.

Learning step:
- Critical for adapter brittleness — log the failure mode and which fixtures.

---

## Scenario: engine paused, forgotten

Symptom:
- Morning digest shows `Engine: rerun=off  news=off` or feed slowly drains and you can't remember pausing.

First move:
- `/status` — confirms.
- `/runbook environment-variables` — sanity-check what each switch does.
- `/resume` — flips both `PULSE_RERUN_ENABLED` and `PULSE_NEWS_INGEST_ENABLED` back to true.

Why this happens:
- Pause from a prior incident, never resumed.
- Manual env-var change in Railway UI that didn't get reverted.

Learning step:
- If pause happened during an incident yesterday, check the `incidents/` directory for the postmortem and confirm the resolution actually included `/resume`.

---

## Scenario: frontend / widget broken (200 OK, page won't render)

Symptom:
- Bot pushed `[ops-bot] CRITICAL — Pulse widget broken for ~Nm  ·  reason: <X>` with `[PREVIEW] [STATUS] [DISMISS]` buttons.
- Or: operator says the widget loads blank / unstyled / partial — even though `/health` and `/api/feed` look healthy.

What it means:
- The widget HTML at the operator's iframe URL is reachable but malformed. `/health` and `/api/feed` only check the API. The bot's widget probe GETs `/`, asserts 200, html content-type, and the presence of two sentinels: the page title and the `<div id="app"` mount point.

Common causes & response:
- **`http 5xx`** — Cloudflare cache poisoning or edge issue. Try `/redeploy` to push a fresh cache; if it's specifically a Railway/Cloudflare incident, wait it out.
- **`missing title sentinel` / `missing mount-point sentinel`** — frontend template error or a bad commit shipped HTML without the expected structure. Roll back via `/redeploy` (it fires the latest deployment again) and if that's the broken commit, escalate to laptop for a true rollback.
- **`non-html content-type`** — something replaced `/` with a JSON or text response. Almost certainly a code issue. Laptop required.
- **`suspiciously short body`** — partial response (proxy timed out mid-flight, edge hiccup). Often transient. Watch one more poll cycle before escalating.

Phone fix:
- Tap `[PREVIEW]` first to confirm the data layer (cards, deep_links) is fine.
- Tap `[STATUS]` for the health line — if `/health` is also failing, this is a backend issue, not just frontend. See `/playbook_health`.
- If only the widget probe fails: tap `[REDEPLOY]` (or send `/redeploy`) and confirm.
- `/snooze frontend 1h` if you're choosing to live with it briefly while triaging.

Escalate to laptop if:
- Two consecutive `/redeploy` cycles don't recover.
- Reason is `non-html content-type` — code bug.
- Reason persists across a redeploy — config or template drift, not transient.

Learning step:
- `/incident start "frontend broken"`; capture the exact `reason` string from the alert; close after recovery. Worth a postmortem entry — frontend breakage is rare but high-impact (user-visible).

---

## Scenario: bad card visible

Symptom:
- Operator or you-eyeballing-`/cards` notice a card with wrong narrative, broken deep-link, or duplicate of another card.

First move:
- Get the card ID. From `/cards`, IDs are in `[brackets]`. From an operator screenshot, the card data may be in their report.
- `/card <id>` to confirm exactly what's published.

Phone fix:
- **Today**: `/blacklist <id>` is **not yet built** (blocked on Pulse `/admin/blacklist` endpoint, see `docs/pulse-session-2026-04-29-followups.md`). Manually setting `PULSE_RERUN_ENABLED=false` halts new candidates but doesn't hide the existing one.
- Workaround until `/blacklist` ships: `/pause` to stop new bad ones; live with the visible one until next cycle expires it (or laptop fix).

Escalate to laptop:
- Direct DB update on `/data/pulse.db` to flip `hidden=1` on the offending row.

Learning step:
- This is a coverage gap. Every bad-card incident before `/blacklist` lands should be logged so the priority is clear.

---

## Scenario: catastrophic — data loss / SQLite corruption

Symptom:
- Pulse `/health` returns 200 but `/api/feed` is empty AND `/breakdown` shows `cards_in_feed_now: 0` AND there's no recent publish in `/logs`.

What it means:
- Either the candidate engine has produced nothing AND snapshot rehydrate found nothing, or the SQLite at `/data/pulse.db` is corrupted/empty.

First move:
- `/logs 50` — look for `[CandidateStore] init` lines and any sqlite errors.

Phone fix:
- **None today.** `/restore` is not yet built. Pulse-side R2 backup + `/admin/restore` endpoint is the next Pulse session.
- Halt user-visible mess: `/pause`. The widget will show whatever it last had.

Laptop required:
- Use Railway CLI to `volumes:download` the corrupted `/data/pulse.db`, inspect, restore from any local copy if available.

Learning step:
- File this aggressively as a known phone-only gap. Once R2 backups + `/admin/restore` ship, this entry rewrites to a `/restore latest` path.

---

## Scenario: bot itself goes silent

Symptom:
- You sent `/status` and got nothing. Or the morning digest didn't arrive.

What it means:
- Either ops-bot is down, ops-bot is up but lost its Telegram connection, or your phone has no internet.

First move:
- Try a few seconds later — Telegram client retries.
- Open Telegram on a different device (laptop browser at https://web.telegram.org).
- If still silent, the bot is down.

Phone fix:
- The bot can't fix itself. UptimeRobot pinging the bot's `/health` is the second-line escape valve — but it's not yet wired.
- Workaround: open Railway in your phone browser → ops-bot service → Restart.

Learning step:
- This is a known gap. Once UptimeRobot is wired (5-min manual setup), an SMS or email arrives within 1 min of the bot going dark.

---

## Scenario: Anthropic or Rogue API down

Symptom:
- Cost is steady (no LLM calls succeeding) but feed is slowly draining, or `/logs` shows `httpx.ConnectError` against `api.anthropic.com` or `prod20392-168426033.msjxk.com`.

First move:
- Confirm scope: is it just one API or both? Anthropic's status page is anthropic-status.com; Rogue/BTI's is operator-internal.
- `/cost detail` — if the engine is failing every cycle, calls count won't increase.

Phone fix:
- Wait. Pulse retries cycle-by-cycle. There's nothing to actually fix from your side.
- `/snooze health 1h` and `/snooze feed 1h` to silence inevitable secondary alerts.
- If the outage exceeds an hour and you want to halt cost burning on retries: `/pause`.

Learning step:
- Note duration and impact. Three Anthropic outages in a quarter would be a signal to keep a fallback LLM provider warm.

---

## Scenario: operator reports widget broken

Symptom:
- A real human (Apuesta Total contact) DMs you on whatever channel saying their iframe is blank, slow, or wrong.

What it means:
- Could be embed token mismatch, theme override conflict, deep-link breakage, or pure operator-side issue (CSP, ad blocker, their CDN).

First move:
- `/embed apuesta-total` — confirm token + domain allowlist + active flag.
- `/preview` — confirm the feed has cards with healthy deep-links.
- Ask the operator for: their domain, the time it broke, whether it was specific cards or all cards.

Phone fix:
- If their domain isn't in `allowed_origins` → `/flag PULSE_EMBED_TOKEN_REQUIRED false` temporarily (security trade-off; reset within 24h) OR add the domain via Pulse admin (laptop).
- If their token is expired/missing → re-share the token from `/embed` output.

Escalate to laptop:
- Theme override conflict.
- New domain registration.
- Anything that needs editing the embeds table directly.

Learning step:
- Every operator report goes in `incidents/` even if minor — this is the highest-signal feedback you have on operator-side breakage. Future state: operator self-reports via `POST /report` (not yet wired).

---

## Learning loop

Every push alert and every operator report is an opportunity to feed the system. The minimum loop:

1. **Alert fires** → bot pushes to Telegram.
2. **Open an incident** — `/incident start "<short title>"`. Slug auto-derives.
3. **As you triage, capture facts** — `/incident note "burnt $X.XX by HH:MM, cause: <bucket>"`. Capture the data you wish you had next time.
4. **Resolve** with the bot commands or laptop, as appropriate.
5. **Close the incident** — `/incident close`. Bot commits a markdown postmortem to `pulse-poc/incidents/<date>-<slug>.md` via GitHub API.
6. **Within 24h**, edit the postmortem (laptop) to add a `## Resolution` section: what fixed it, what you'd do differently, whether a coverage gap was exposed.
7. **Once a quarter**, scan the `incidents/` directory and look for repeats. Repeated symptoms → next bot iteration or next Pulse PR.

Why this matters single-founder: nobody else will write the postmortem. If you don't capture the moment, the lesson is gone within hours.

Coverage gaps the loop should specifically watch for:
- New scenarios that don't appear in this playbook → add a section.
- Bot commands you wished existed → file them.
- Push alerts that fired but didn't actually need attention → tune the threshold.

---

## When to wake up vs sleep through

You operate solo. Some things genuinely need you at 3am; most don't. Default policy:

| Alert | 3am response |
|---|---|
| Cost $1 ladder | sleep |
| Cost $2 ladder | sleep |
| Cost $2.95 ladder | engine self-kills at $3 — sleep |
| Pulse `/health` 5xx >2min | wake **only if** the operator has paying users mid-event; otherwise sleep, look at it in the morning |
| Deploy FAILED | sleep (you didn't deploy at 3am) |
| Feed unhealthy | sleep |
| Deep-link 3/5 failing | sleep — operator URL pattern doesn't change overnight |
| Operator reports widget broken | wake (low frequency, high impact on relationship) |
| Sentry new exception (when wired) | sleep — review at 9am |

Default action when waking is unclear: send `/snooze all 6h` and check at 9am.

---

## Adding to this playbook

When you encounter a new scenario, add a `## Scenario: <name>` section here matching the existing template:

- Symptom
- What it means
- First move
- Common causes (if multiple)
- Phone fix
- Escalate to laptop if
- Learning step

Then commit. The bot's `/playbook` re-fetches every hour from GitHub raw, so a fresh commit shows up without redeploying.
