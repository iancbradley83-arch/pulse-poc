# Security Policy

## Reporting a vulnerability

Please **do not file a public GitHub issue** for security reports.

Email: **security@pulse-poc** _(update with real contact)_

We aim to:
- Acknowledge the report within **2 business days**
- Ship a fix for critical issues within **30 days**
- Credit reporters in release notes (unless you prefer anonymity)

## Scope

In scope:
- The production service at `https://pulse-poc-production.up.railway.app`
- Any code in this repository
- Dependency vulnerabilities we haven't yet patched

Out of scope:
- Social engineering / phishing
- DoS via overwhelming volume (rate limits exist; we'd rather hear about bypasses)
- Findings requiring physical access to a developer machine

## What we're doing already

- Dependency CVE scan against requirements.txt via pip-audit (manual cadence today, CI pending)
- Security-headers middleware on every response (HSTS, CSP, nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy) — implemented as pure ASGI for perf parity
- OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) disabled in production
- Explicit CORS allowlist required in production via `PULSE_CORS_ALLOWED_ORIGINS`
- Per-IP rate limiting via slowapi (overridable with `PULSE_RATE_LIMIT_DEFAULTS`)
- Regular end-to-end audits via the `/audit-prod` tool — cadence weekly + before releases
