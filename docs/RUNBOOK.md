# pulse-poc Runbook

Operational reference for incidents, deploys, and routine maintenance.

---

## Constants

| Item | Value |
|---|---|
| Production URL | https://pulse-poc-production.up.railway.app |
| Railway project ID | `e8f10296-5781-4dcc-a1ff-21a4375f9ae8` |
| Railway service ID | `0b99fc7c-0018-477e-aad9-c5b4150d89f8` |
| Railway environment ID | `1a42ace7-f2f7-4694-9310-01ee60f16e2d` |
| GitHub | https://github.com/iancbradley83-arch/pulse-poc |
| Deploy path | `main` → Railway auto-deploys |
| Stack | FastAPI + uvicorn on Railway (Docker build from `backend/Dockerfile`) |

`RAILWAY_API_TOKEN` lives as an env-only secret. See `~/.claude/skills/railway-status/SKILL.md` for use.

### **IMPORTANT: only one `requirements.txt`**

The Dockerfile does `COPY backend/requirements.txt .` — **`backend/requirements.txt` is the canonical deps file.** If a stray `./requirements.txt` shows up at the repo root, delete it; it's ignored by the build and will cause confusion. (This happened on 2026-04-22 and took the site down for ~15 min — commit history will show an "emergency" PR.)

---

## Environment variables

| Name | Purpose | Default |
|---|---|---|
| `PULSE_CORS_ALLOWED_ORIGINS` | Comma-separated CORS origin allowlist | (empty → deny cross-origin) |
| `PULSE_EXPOSE_DOCS` | Re-enable `/docs` in prod | `false` |
| `PULSE_CSP_REPORT_ONLY` | Flip CSP to report-only | `false` (enforcing) |
| `PULSE_RATE_LIMIT_DEFAULTS` | slowapi rules, comma-separated | `300/minute,20/second` |
| `PULSE_LOG_LEVEL` | Logger level | `INFO` |
| `PULSE_DATA_SOURCE` | `mock` or `rogue` (live catalogue) | `mock` |
| `PULSE_NEWS_INGEST_ENABLED` | Kill switch for Anthropic spend on news | (defaults on) |
| `ROGUE_CONFIG_JWT` | Rogue API auth — **rotate periodically** | required for rogue mode |
| `ANTHROPIC_API_KEY` | Claude Haiku 4.5 for news scouting | required |
| `RAILWAY_ENVIRONMENT` | Set automatically by Railway | — |
| `RAILWAY_GIT_COMMIT_SHA` | Set automatically — surfaced at `/health` (not currently; TODO) | — |

---

## Common tasks

### Verify a deploy

```bash
curl https://pulse-poc-production.up.railway.app/health
# Expected: {"ok": true}
```

Note: current `/health` is trivial. Consider enriching with the short git SHA so `curl` alone confirms the deploy landed (see polysportsbook for the pattern).

### Check deploy status via Railway API

```bash
curl -sS https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"query { deployments(first: 3, input: { projectId: \"e8f10296-5781-4dcc-a1ff-21a4375f9ae8\", serviceId: \"0b99fc7c-0018-477e-aad9-c5b4150d89f8\" }) { edges { node { id status createdAt meta } } } }"}'
```

Status: `INITIALIZING` → `BUILDING` → `DEPLOYING` → `SUCCESS`, or `FAILED` / `CRASHED`.

### Pull runtime logs

```bash
DEP_ID="<from deployments query>"
curl -sS https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"query\":\"query { deploymentLogs(deploymentId: \\\"$DEP_ID\\\", limit: 500) { message severity timestamp } }\"}"
```

### Pull build logs (when a deploy fails)

```bash
curl -sS https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"query\":\"query { buildLogs(deploymentId: \\\"$DEP_ID\\\", limit: 500) { message severity } }\"}"
```

### Rollback

```bash
gh pr revert <pr-number>
gh pr merge <revert-pr> --squash
# Railway auto-deploys the revert (~3–4 min)
```

Emergency: Railway UI → Deployments tab → click prior SUCCESS → "Redeploy". Instant.

### Tune rate limits

```bash
# Set on Railway: PULSE_RATE_LIMIT_DEFAULTS=600/minute,50/second
# Redeploys automatically (~3–4 min)
```

### Trigger an on-demand candidate-engine rerun

```bash
curl -sS -X POST https://pulse-poc-production.up.railway.app/admin/rerun
# Fire-and-forget — Railway's edge caps HTTP at 60s so the engine response
# comes back via logs, not the response.
```

---

## Incident playbook

### Site returns 502 / /health times out

1. Check deploy status. If `BUILDING` / `DEPLOYING`, wait.
2. `status=SUCCESS` but 502: cold start. **pulse-poc cold start is ~4 minutes** (catalogue load + LLM scout + rewriter). Be patient.
3. Still 502 after 5 min: pull runtime logs, grep for `Traceback`, `Error`, `FAILED`.
4. If `ModuleNotFoundError` in logs → **the wrong `requirements.txt` was edited** (see Constants section).
5. If startup failure persists: roll back via Railway UI redeploy-prior.

### Deploy is stuck in BUILDING > 5 minutes

Pulse-poc's Docker build (python:3.11-slim + playwright chromium + apt deps) usually takes 2–3 min. > 5 min = something is pulling a fresh image or there's a Railway platform slowdown.

### Build time < 30 seconds

Means pip-install layer was cached. If you just updated `backend/requirements.txt`, verify the container actually has the new deps — grep build logs for `Successfully installed <pkg>-<version>`. If the version is stale, touch the Dockerfile to force a cache bust.

### `/health` returns 200 but the feed is empty

The candidate engine hasn't run its first cycle yet. Cold boot runs it once; then APScheduler fires every 4h. On-demand: POST `/admin/rerun`.

### Anthropic spend spikes

Set `PULSE_NEWS_INGEST_ENABLED=false` on Railway → instant kill switch for Haiku + web-search calls. Engine still runs but skips news scouting.

### Rate limits trigger false positives from monitors

`/health` is exempt. If something else is getting throttled, loosen via `PULSE_RATE_LIMIT_DEFAULTS`.

---

## Audit cadence

Run `/audit-prod` weekly or before releases. Full battery:

```bash
SCRIPT="$HOME/.claude/skills/audit-prod/scripts/audit.py"
URL="https://pulse-poc-production.up.railway.app"
HOST="pulse-poc-production.up.railway.app"
STAMP=$(date +%Y%m%d-%H%M%S)

python3 "$SCRIPT" security --url "$URL" --out /tmp/pp-$STAMP-sec.json
python3 "$SCRIPT" perf-baseline --url "$URL" --samples 10 --out /tmp/pp-$STAMP-baseline.json
python3 "$SCRIPT" tls --hostname "$HOST" --out /tmp/pp-$STAMP-tls.json
python3 "$SCRIPT" dns --domain "$HOST" --out /tmp/pp-$STAMP-dns.json
python3 "$SCRIPT" deps --requirements ~/pulse-poc/backend/requirements.txt --out /tmp/pp-$STAMP-deps.json
python3 "$SCRIPT" ops --repo ~/pulse-poc --url "$URL" --out /tmp/pp-$STAMP-ops.json

python3 "$SCRIPT" report \
  --security /tmp/pp-$STAMP-sec.json \
  --baseline /tmp/pp-$STAMP-baseline.json \
  --tls /tmp/pp-$STAMP-tls.json \
  --dns /tmp/pp-$STAMP-dns.json \
  --deps /tmp/pp-$STAMP-deps.json \
  --ops /tmp/pp-$STAMP-ops.json \
  --out ~/audit-reports/pulse-poc-$STAMP.md
```

Target state: **0 critical, 0 high** findings across all phases. Current: `/health` trivial body + known "no branch protection" + known "no observability" gaps.

---

## Known gaps (tracked, not yet fixed)

- **`/health` body is trivial** (`{"ok": true}`). Enrich with `service`, `version` (Railway git SHA), `environment` for unambiguous deploy verification from curl.
- **No observability wiring** (Sentry / structured logs / metrics). Gating on getting a Sentry DSN set up.
- **No CI workflow** (`.github/workflows/`). Need to mirror polysportsbook's `lint-test-audit` job.
- **No main-branch protection.** Enable once CI is in place.
- **Candidate engine runs in-process** → can't scale to >1 replica without leader election. Planned as a separate workstream.
- **SQLite on Railway container disk.** Survives because mounted on volume (`/data`), but single-writer means no horizontal scaling.

---

## Contacts

| Role | Who |
|---|---|
| Primary maintainer | Ian Bradley (@iancbradley83-arch) |
| Security reports | See SECURITY.md |
