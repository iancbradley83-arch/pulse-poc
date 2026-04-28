"""
Environment variable loading and validation for ops-bot.
All config is sourced from env vars — nothing on disk, nothing in git.
"""
import os
from typing import List


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required env var {name!r} is not set")
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def get_telegram_token() -> str:
    return _require("TELEGRAM_BOT_TOKEN")


def get_allowed_chat_ids() -> List[int]:
    raw = _optional("OPS_BOT_ALLOWED_CHAT_IDS", "")
    if not raw.strip():
        return []
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                raise RuntimeError(
                    f"OPS_BOT_ALLOWED_CHAT_IDS contains non-integer value: {part!r}"
                )
    return ids


def get_pulse_base_url() -> str:
    return _optional("PULSE_BASE_URL", "https://pulse-poc-production.up.railway.app")


def get_pulse_admin_user() -> str:
    return _optional("PULSE_ADMIN_USER", "")


def get_pulse_admin_pass() -> str:
    return _optional("PULSE_ADMIN_PASS", "")


def get_railway_api_token() -> str:
    return _optional("RAILWAY_API_TOKEN", "")


def get_health_port() -> int:
    raw = _optional("PORT", "8080")
    try:
        return int(raw)
    except ValueError:
        return 8080


# Railway project constants — these do not change for Stage 1.
RAILWAY_PROJECT_ID = "e8f10296-5781-4dcc-a1ff-21a4375f9ae8"
RAILWAY_SERVICE_ID = "0b99fc7c-0018-477e-aad9-c5b4150d89f8"
RAILWAY_ENVIRONMENT_ID = "1a42ace7-f2f7-4694-9310-01ee60f16e2d"

# Cost ladder thresholds in USD.
COST_THRESHOLDS = [1.00, 2.00, 2.95]
DAILY_BUDGET = 3.00

# Alert polling interval in seconds.
COST_POLL_INTERVAL = 300
