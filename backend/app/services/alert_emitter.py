"""alert_emitter — thin, fire-and-forget critical alert shim.

Why a shim and not the shared `~/alerts` package: Railway only deploys
the `pulse-poc` repo. The local Python package at `~/alerts` is not on
the deploy filesystem, so `from alerts import ...` would ImportError at
runtime. Rather than vendoring it (and synchronising every channel
plugin), we keep a 50-line in-process emitter here that:

  * Logs every alert at WARNING level (Sentry's FastApiIntegration
    captures WARNINGs as breadcrumbs and surfaces criticals).
  * Optionally POSTs a JSON body to a webhook (`PULSE_ALERTS_WEBHOOK_URL`)
    via stdlib urllib — no new top-level dependency.
  * Dedups within a single UTC day on a `(date, dedup_key)` tuple, so
    re-firing tripwires don't spam the operator. The set resets at UTC
    midnight by virtue of the date prefix changing.
  * Never raises. All exceptions on the network path are caught and
    logged. The engine's hot path stays clean even if the webhook URL
    is malformed, the network is down, or the operator has not yet
    wired a destination.

Schema mirrors the `~/alerts` package so a future migration is a no-op:

    {
      "level": "critical",
      "title": "...",
      "body": "...",
      "timestamp": "<ISO-8601 UTC>",
      "project": "pulse"
    }

The webhook target is operator-chosen — Telegram bot URL, Slack
incoming webhook, custom relay, whatever. The body is generic JSON that
all three accept (Slack via blocks, Telegram via custom adapter, etc.).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)

_WEBHOOK_TIMEOUT_SECONDS = 5
_PROJECT_NAME = "pulse"


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class AlertEmitter:
    """Module-singleton. Tracks (utc_date, dedup_key) pairs to suppress repeats.

    The `_seen` set is intentionally simple — we don't expect more than
    a handful of dedup keys per day, and they roll naturally because
    every key is prefixed with a UTC date string. No background thread
    or scheduled prune is needed.
    """

    _seen: Set[Tuple[str, str]] = set()
    _lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        """Test-only: drop the dedup set so each test starts clean."""
        with cls._lock:
            cls._seen.clear()

    @classmethod
    def emit_critical(
        cls,
        title: str,
        body: str,
        dedup_key: Optional[str] = None,
    ) -> None:
        """Fire a critical alert exactly once per UTC day per dedup_key.

        - Always logs at WARNING (Sentry breadcrumb).
        - POSTs to `PULSE_ALERTS_WEBHOOK_URL` if set.
        - Swallows every exception. Never raises into the caller.
        """
        try:
            today = _today_utc()
            if dedup_key is not None:
                key = (today, str(dedup_key))
                with cls._lock:
                    if key in cls._seen:
                        return
                    cls._seen.add(key)

            logger.warning(
                "[alert] CRITICAL %s — %s (dedup_key=%s)",
                title, body, dedup_key,
            )

            webhook = os.getenv("PULSE_ALERTS_WEBHOOK_URL", "").strip()
            if not webhook:
                return

            payload = {
                "level": "critical",
                "title": str(title),
                "body": str(body),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "project": _PROJECT_NAME,
            }
            # Fire-and-forget on a daemon thread so a slow webhook never
            # blocks the engine path. No join — process exit drops the
            # thread without complaint.
            t = threading.Thread(
                target=_post_webhook,
                args=(webhook, payload),
                daemon=True,
                name="pulse-alert-emitter",
            )
            t.start()
        except Exception as exc:  # pragma: no cover — defensive last resort
            try:
                logger.warning("[alert] emit_critical swallowed exception: %s", exc)
            except Exception:
                pass


def _post_webhook(url: str, payload: dict) -> None:
    """Best-effort JSON POST. Catches all exceptions; never re-raises."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SECONDS) as resp:
            # Read+discard to free the socket; status logged at debug.
            resp.read()
            logger.debug(
                "[alert] webhook POST %s → %s", url, getattr(resp, "status", "?"),
            )
    except urllib.error.URLError as exc:
        logger.warning("[alert] webhook URL error: %s", exc)
    except Exception as exc:
        logger.warning("[alert] webhook unexpected error: %s", exc)


def emit_critical(
    title: str,
    body: str,
    dedup_key: Optional[str] = None,
) -> None:
    """Module-level shortcut to `AlertEmitter.emit_critical`."""
    AlertEmitter.emit_critical(title, body, dedup_key=dedup_key)
