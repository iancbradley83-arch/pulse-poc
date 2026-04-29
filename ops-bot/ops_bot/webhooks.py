"""
Webhook handler scaffolding (Stage 4 lite).

Two inbound endpoints, both gated by query-param tokens:

  POST /sentry?token=<SENTRY_WEBHOOK_TOKEN>
    Receives Sentry event payloads. Summarises and pushes to all allowed chat IDs.

  POST /report?slug=<slug>&token=<OPERATOR_REPORT_TOKEN>
    Receives operator-initiated reports. Pushes to chat as:
    [<slug>] reported: <subject> — <body>

Both endpoints:
  - Return 401 if the token is wrong or missing.
  - Return 503 if the required env var is not configured.
  - Never raise to the aiohttp framework — all errors returned as JSON.

These handlers are INERT until Ian:
  1. Sets SENTRY_WEBHOOK_TOKEN and/or OPERATOR_REPORT_TOKENS on Railway.
  2. Attaches a public domain to the ops-bot service.
  3. Configures the webhook URL in Sentry / hands the /report URL to operators.
"""
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Type alias for the broadcast callable passed in from main.py.
BroadcastFn = Callable[[str], Awaitable[None]]


def _get_sentry_token() -> Optional[str]:
    return os.environ.get("SENTRY_WEBHOOK_TOKEN") or None


def _get_operator_tokens() -> Dict[str, str]:
    """
    Parse OPERATOR_REPORT_TOKENS env var.

    Expected format: JSON object {"slug": "token", ...}
    Returns empty dict if unset or unparseable.
    """
    raw = os.environ.get("OPERATOR_REPORT_TOKENS", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
        logger.warning("webhooks: OPERATOR_REPORT_TOKENS is not a JSON object")
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("webhooks: failed to parse OPERATOR_REPORT_TOKENS: %s", exc)
        return {}


def _summarise_sentry(payload: Dict[str, Any]) -> str:
    """
    Extract key fields from a Sentry event webhook payload and return a
    human-readable summary.

    Sentry's webhook shape can vary by integration version; we extract
    conservatively and fall back to raw data.
    """
    # Sentry webhook wraps the event in different keys depending on type.
    # Try both "event" (issue alert) and top-level keys (issue-created hook).
    event = payload.get("event") or payload
    issue = payload.get("data", {}).get("issue") or {}

    event_id = (
        event.get("event_id")
        or payload.get("id")
        or issue.get("id")
        or "unknown"
    )
    title = (
        event.get("title")
        or payload.get("message")
        or issue.get("title")
        or "(no title)"
    )
    level = (
        event.get("level")
        or payload.get("level")
        or issue.get("level")
        or "error"
    ).upper()
    project = (
        event.get("project")
        or payload.get("project_slug")
        or payload.get("project")
        or "unknown"
    )
    url = (
        payload.get("url")
        or issue.get("permalink")
        or event.get("web_url")
        or ""
    )

    lines = [
        f"[ops-bot] Sentry {level} — {title}",
        f"event: {event_id}  project: {project}",
    ]
    if url:
        lines.append(url)
    return "\n".join(lines)


async def handle_sentry(request: web.Request, broadcast: BroadcastFn) -> web.Response:
    """
    POST /sentry?token=<SENTRY_WEBHOOK_TOKEN>

    Validates token, summarises payload, broadcasts to all chat IDs.
    """
    try:
        sentry_token = _get_sentry_token()
        if sentry_token is None:
            return web.json_response(
                {"ok": False, "error": "feature not configured — set SENTRY_WEBHOOK_TOKEN"},
                status=503,
            )

        provided_token = request.rel_url.query.get("token", "")
        if not provided_token or provided_token != sentry_token:
            logger.warning("webhooks: sentry — invalid or missing token")
            return web.json_response(
                {"ok": False, "error": "invalid token"},
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        summary = _summarise_sentry(payload)
        logger.info("webhooks: sentry event received — broadcasting")

        try:
            await broadcast(summary)
        except Exception as exc:
            logger.error("webhooks: sentry broadcast failed: %s", exc)

        return web.json_response({"ok": True})

    except Exception as exc:
        logger.error("webhooks: unexpected error in sentry handler: %s", exc)
        return web.json_response({"ok": False, "error": "internal error"}, status=500)


async def handle_report(request: web.Request, broadcast: BroadcastFn) -> web.Response:
    """
    POST /report?slug=<slug>&token=<OPERATOR_REPORT_TOKEN>

    Body: form-encoded with fields "subject" and "body".
    """
    try:
        operator_tokens = _get_operator_tokens()
        if not operator_tokens:
            return web.json_response(
                {"ok": False, "error": "feature not configured — set OPERATOR_REPORT_TOKENS"},
                status=503,
            )

        slug = request.rel_url.query.get("slug", "")
        provided_token = request.rel_url.query.get("token", "")

        if not slug or not provided_token:
            return web.json_response(
                {"ok": False, "error": "missing slug or token"},
                status=401,
            )

        expected_token = operator_tokens.get(slug)
        if expected_token is None or provided_token != expected_token:
            logger.warning("webhooks: report — invalid token for slug %r", slug)
            return web.json_response(
                {"ok": False, "error": "invalid token"},
                status=401,
            )

        try:
            data = await request.post()
        except Exception:
            data = {}

        subject = str(data.get("subject", "")).strip() or "(no subject)"
        body = str(data.get("body", "")).strip() or "(no body)"

        msg = f"[{slug}] reported: {subject} — {body}"
        logger.info("webhooks: report from %r — broadcasting", slug)

        try:
            await broadcast(msg)
        except Exception as exc:
            logger.error("webhooks: report broadcast failed: %s", exc)

        return web.json_response({"ok": True})

    except Exception as exc:
        logger.error("webhooks: unexpected error in report handler: %s", exc)
        return web.json_response({"ok": False, "error": "internal error"}, status=500)
