# Pulse — operator handoff: Apuesta Total (Peru)

This doc covers everything Apuesta Total's tech / product team needs to embed Pulse and go live. Pulse is the **content widget** — it surfaces narrative-first bet recommendations and deep-links into the Apuesta Total slip when a user taps a card.

If you're inside FIRST and onboarding a different operator, see also `docs/operator-adapter.md` (multi-tenant config drop) and `docs/cloudflare-setup.md`.

## What Apuesta Total gets

| | |
|---|---|
| Product | Pulse — narrative-driven bet content widget |
| Surface | iframe embedded in any Apuesta Total page (homepage, sportsbook lobby, fixture pages) |
| Refresh cadence | 60-second polling (no WebSocket needed on operator side) |
| Languages (v1) | `es-pe` |
| Sports (v1) | International soccer — EPL, La Liga, Bundesliga, Serie A, Ligue 1, Champions League, Europa League |
| Bet flow | Tap card → Apuesta Total slip opens with selections pre-loaded via Kmianko bscode |
| Money handling | None. Pulse never sees the wager. Apuesta Total handles all KYC, AML, settlement, payouts. |

## Embed contract

Operator side, drop in:

```html
<iframe
  src="https://pulse.first.bet/?embed_token=APUESTA_TOTAL_TOKEN_HERE&powered_by=true"
  width="100%"
  height="700"
  loading="lazy"
  referrerpolicy="strict-origin-when-cross-origin"
  allow="clipboard-write"
  style="border: 0; max-width: 480px;"
></iframe>
```

Where `APUESTA_TOTAL_TOKEN_HERE` is the embed token issued from `/admin/embeds`. We provision one token per environment (staging, production); rotate on demand.

### URL parameters

| Param | Required | Purpose |
|---|---|---|
| `embed_token` | yes (when `PULSE_EMBED_TOKEN_REQUIRED=true`) | Authenticates the embed against the per-operator allowlist |
| `powered_by` | no, default `true` | `false` removes the "Powered by Pulse" footer badge |
| `theme` | no, default `dark` | `dark` or `light` — light is wave 4, currently a placeholder |
| `lang` | no, default `es-pe` | Reserved for future multi-locale support |

### Domain allowlist

Pulse rejects iframe loads from origins not in the embed's `allowed_origins` list. For Apuesta Total, the seeded list is:

- `*.apuestatotal.com.pe`
- `apuestatotal.pe`
- `localhost` (dev only — remove before public launch)
- `pulse-poc-production.up.railway.app` (FIRST staging surface)

Update via `/admin/embeds/apuesta-total` form when adding/removing domains.

## Deep-link handoff (the bet flow)

When a user taps a card's CTA, Pulse:

1. Reads the pre-minted `deep_link` from the card payload (Kmianko bscode embedded). Format:
   `https://www.apuestatotal.com.pe/sports/<route>?bscode=<MINTED_BSCODE>`
2. Opens `deep_link` in the parent window (`target="_top"` because we're inside an iframe).
3. The Apuesta Total slip page reads the `bscode`, validates, and pre-loads the selections.
4. User completes the wager natively on Apuesta Total. Pulse never sees the bet.

We pre-mint bscodes server-side via the documented Kmianko endpoints (see `~/Downloads/rogue-api-mcp/BETSLIP_SHARE_ADDENDUM.md`). Single bets, BetBuilders, and combos all work. The live frontend is Playwright-verified for round-trip slip population.

### What the operator must NOT do

- Do **not** strip the `bscode` query param via marketing tracking or affiliate redirects — selections will be empty.
- Do **not** route the click through a same-origin redirect that drops query params.
- Do **not** allowlist Pulse to display real-money balance, KYC status, or any account data — Pulse is anonymous content only.

## Theming

Pulse uses CSS custom properties on `:root`. Per-embed overrides are wave-4 work; for v1 the operator can override globally in their host page only (since iframe styles don't bleed in by default).

Tokens (subject to small additions):

```
--pulse-bg
--pulse-bg-card
--pulse-text
--pulse-text-muted
--pulse-accent
--pulse-accent-fg
--pulse-success
--pulse-danger
--pulse-warning
--pulse-border
--pulse-radius-sm
--pulse-radius-md
--pulse-shadow
--pulse-font
--pulse-font-size-base
--pulse-space-1 ... --pulse-space-6
```

For Apuesta Total v1 the default dark theme matches their existing site palette closely enough that no overrides are needed.

## Observability + status

| Surface | URL |
|---|---|
| Public status | `https://stats.uptimerobot.com/<id>` (after UptimeRobot setup) |
| Cost dashboard | `https://pulse-poc-production.up.railway.app/admin/cost` (FIRST internal) |
| Embed admin | `https://pulse-poc-production.up.railway.app/admin/embeds` (FIRST internal) |

## SLA + cost guardrails (Pulse side)

| | |
|---|---|
| Target uptime | 99% (43 min/month allowed downtime) — soft, no penalty contract |
| Daily LLM budget | $3.00 USD (engine self-pauses at the cap; cards keep serving from snapshot) |
| Recovery cost | $0 — pause is automatic, resume is one env-var flip |
| Incident contact | `alerts@first.bet` (TBD) |

## Going live — checklist

1. [ ] FIRST: provision Apuesta Total embed token via `/admin/embeds`
2. [ ] FIRST: confirm `*.apuestatotal.com.pe` is in the embed's `allowed_origins`
3. [ ] FIRST: flip `PULSE_EMBED_TOKEN_REQUIRED=true` on Railway
4. [ ] FIRST: add `pulse.first.bet` to `PULSE_CORS_ALLOWED_ORIGINS`
5. [ ] FIRST: Cloudflare in front of `/api/feed` (`docs/cloudflare-setup.md`)
6. [ ] FIRST: UptimeRobot monitors live (`docs/uptimerobot-setup.md`)
7. [ ] FIRST: Sentry DSN set on Railway (`PULSE_SENTRY_DSN=...`)
8. [ ] Apuesta Total: drop the iframe snippet on a staging page, verify cards render
9. [ ] Apuesta Total: tap a card, verify slip pre-loads with the right selections
10. [ ] Apuesta Total: validate UTM / affiliate trackers don't strip `bscode`
11. [ ] Both: run `/admin/cost` for 48h post-launch, confirm spend <$1/day
12. [ ] Both: agree on alert routing — who gets the UptimeRobot pings, who watches `/admin/cost`

## What's deferred to post-launch

- Per-embed theme overrides (wave 4)
- Multi-tenant config (when a second operator signs)
- Live in-play markets (Stage 6+)
- Per-anon personalisation (data is collected; ML pass later)
- Script-tag SDK alternative to iframe (lighter integration footprint)
- Pen-test (post-launch via Detectify or similar, $100/mo)
- Authenticated `/admin/*` surfaces (currently open inside the FIRST network only)

## Contact

- Pulse engineering: Ian Bradley
- Cost / engine alerts: route to FIRST's shared Telegram alerts channel (see `~/.alerts/config.json`)
- Operator support: TBD — agree before launch

---

## Open commercial / launch questions for Apuesta Total

**Status (2026-05-03):** Pulse-side iframe contract, deep-link pre-mint, embed admin, and `*.apuestatotal.com.pe` allowlist are ready. Going-live is unblocked on our side. The four answers below decide the launch shape and the commercial framing — none require code from either team.

| # | Question | Why it matters |
|---|---|---|
| 1 | **Surface placement.** Which page is the iframe going on first — homepage hero, sportsbook lobby, or fixture detail pages? Single surface or multiple? | Decides expected impressions, refresh cadence, and whether the 480px max-width default needs a wider variant. |
| 2 | **Commercial structure.** Free pilot for the first 90 days then revenue-share, fixed monthly, or pure pilot with no commercials? If revenue-share: % rate and attribution mechanism (UTM tag on `bscode` link, or AT-side tracking). | Sets the contractual frame and tells us whether to build a click-attribution surface (we don't have one yet). |
| 3 | **Content review.** Does AT need editorial / regulatory review of the `es-pe` card copy before public launch, or is the current LLM-generated output acceptable for a soft launch on a low-traffic surface? | Determines whether we ship hidden behind an internal AT staging path first, or land on a real customer-facing page from day one. |
| 4 | **UTM / affiliate redirect chain.** Will the click from a Pulse card to `apuestatotal.com.pe/sports/<route>?bscode=...` pass through any tracking redirect that might strip the `bscode` query param? Testable in one hour on AT staging — we can pre-mint a bscode and click it together. | If `bscode` gets stripped, the slip lands empty and the user re-experiences a broken bet flow on every card click. This is the highest-risk launch failure mode. |

Once the four are answered (single email round-trip to AT's tech + commercial leads), the launch checklist above becomes immediately actionable.
