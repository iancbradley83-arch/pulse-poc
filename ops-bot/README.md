# pulse-ops-bot (Stage 1)

Telegram operations console for Pulse. Full design rationale and Stage 2-4 roadmap: `ops-bot/DESIGN.md`.

Built with aiogram 3.27.0 (the spec called for 3.13.x but 3.27.0 is the first 3.x release with wheels that support the pydantic version required on Python 3.14+; the API surface used in Stage 1 is identical across both versions).

Stage 1 ships read-only commands (`/help`, `/status`, `/cost`) and a cost-ladder push alert ($1 / $2 / $2.95 daily thresholds). The bot runs as a separate Railway service so a Pulse outage does not take the bot down.

---

## Local dev

Copy the env var template and fill in values:

```
TELEGRAM_BOT_TOKEN=<from @BotFather>
OPS_BOT_ALLOWED_CHAT_IDS=         # leave blank first boot; fill after allowlisting yourself
PULSE_BASE_URL=https://pulse-poc-production.up.railway.app
PULSE_ADMIN_USER=                  # set when /admin/* basic-auth lands (PR queue)
PULSE_ADMIN_PASS=
RAILWAY_API_TOKEN=<account token>
PORT=8080
```

Save as `ops-bot/.env`. It is gitignored — never commit it.

Install dependencies and run:

```bash
cd pulse-poc/ops-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

The bot starts long-polling Telegram and exposes `GET /health` on port 8080.

Run tests:

```bash
pytest tests/ -v
```

---

## Deployment on Railway

### Create the service

1. Open Railway project `e8f10296-5781-4dcc-a1ff-21a4375f9ae8`.
2. Click **New Service** → **Empty Service**.
3. Connect the GitHub repo `pulse-poc` (same repo as Pulse).
4. **Settings → Source** → set root directory to `/ops-bot`.
5. **Settings → Build** → Dockerfile is auto-detected from `ops-bot/Dockerfile`.
6. **Settings → Networking** → no public domain needed (bot uses long-poll, not webhook).
7. **Variables** → add the following env vars (see table below).
8. Click **Deploy**.

### Variables to set

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `OPS_BOT_ALLOWED_CHAT_IDS` | Leave blank initially (see first-boot steps) |
| `PULSE_BASE_URL` | `https://pulse-poc-production.up.railway.app` |
| `PULSE_ADMIN_USER` | Match Pulse's basic-auth user (set when that PR lands) |
| `PULSE_ADMIN_PASS` | Match Pulse's basic-auth password (set when that PR lands) |
| `RAILWAY_API_TOKEN` | Railway account token |
| `PORT` | Railway sets this automatically; do not override |

---

## First-boot allowlisting

On first deploy `OPS_BOT_ALLOWED_CHAT_IDS` is blank, so the bot rejects everyone.

1. Open Telegram and send any message to the bot.
2. Open Railway → ops-bot service → **Logs**.
3. Grep for `unauthorised chat:`. The number after it is your chat ID.
4. Set `OPS_BOT_ALLOWED_CHAT_IDS=<that id>` in Railway variables.
5. Redeploy (Railway auto-deploys on variable change, or click Deploy).
6. The bot sends `[ops-bot] online — restart` to your chat, confirming it works.

---

## Env var reference

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `OPS_BOT_ALLOWED_CHAT_IDS` | No | Comma-separated Telegram chat IDs that may use the bot. Empty = reject all. |
| `PULSE_BASE_URL` | No | Pulse backend URL (defaults to production Railway URL) |
| `PULSE_ADMIN_USER` | No | HTTP basic-auth username for `/admin/*` endpoints |
| `PULSE_ADMIN_PASS` | No | HTTP basic-auth password for `/admin/*` endpoints |
| `RAILWAY_API_TOKEN` | No | Railway account token for deploy status + env var queries |
| `PORT` | No | Health server port (Railway sets this automatically; default 8080) |

---

## Stage 2 / 3 / 4

Scope for later stages is in `ops-bot/DESIGN.md` section 9. Do not implement those in this service until the corresponding PR is dispatched.
