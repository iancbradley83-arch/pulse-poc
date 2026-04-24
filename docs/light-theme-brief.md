# Light theme — design + engineering brief

This is what the proper light-theme pass needs. The quick 15-min token-swap failed because `styles.css` has ~20 hardcoded dark colours in visible surfaces (header gradient, card base gradient, filter-strip backdrop, chip borders, skeleton shimmer, logomark). A real light theme is a design task, not a token swap.

**Estimate: 1 day of focused work** (~3-4 hours agent time + design iteration + browser verification).

## What the failed attempt taught us

PR #57 flipped `:root` CSS variables under `[data-theme="light"]` — `--bg-app`, `--text-primary`, `--surface-card`, etc. Result: the body background went light, but every component using hardcoded colours stayed dark. Black text flipped onto still-dark cards → invisible. See the screenshot in session history for the specific pathology.

Reverted in PR #58.

## Scope of the real pass

### Phase 1 — Token audit (must come first)

Go through `backend/app/static/styles.css` and replace every hardcoded colour with a CSS variable. Current hardcoded offenders (from `grep "background.*(#0|#1|rgba"`):

| Line | File | Current | Needs to become |
|---|---|---|---|
| ~159 | header gradient | `linear-gradient(180deg, rgba(15,15,20,0.95), rgba(10,10,15,0.85))` | `var(--header-bg)` (new token) |
| ~186 | `.mark-core` | `#0a0a0f` | `var(--logo-core-bg)` |
| ~224 | `.filter-strip` | `rgba(10,10,15,0.88)` | `var(--filter-strip-bg)` |
| ~246 | `.filter-divider` | `rgba(255,255,255,0.08)` | `var(--border-subtle)` (already exists) |
| ~258 | `.chip` border | `rgba(255,255,255,0.08)` | `var(--border-subtle)` |
| ~259 | `.chip` text | `rgba(255,255,255,0.55)` | `var(--text-muted)` |
| ~319 | chip active bg | `rgba(255,255,255,0.08)` | `var(--chip-active-bg)` |
| ~398 | `.card-hero` | `linear-gradient(180deg, #13131b 0%, #0b0b10 100%)` | `var(--card-bg-gradient)` |
| ~537 | Pulse Angle pill | `linear-gradient(rgba(124,92,255,0.15), rgba(124,92,255,0.05))` | `var(--angle-pill-bg)` (derives from `--accent`) |
| ~546 | card glyph glow | `radial-gradient(... rgba(198,255,61,0.08) ...)` | `var(--card-glyph-glow)` (derives from `--win`) |
| ~584, 657, 715, 845 | various `rgba(255,255,255,0.03-0.07)` surfaces | odds-tile, stats, etc. | `var(--overlay-soft)` and family |
| ~772 | Specific dark surface | `#11141c` | `var(--surface-elevated)` |

Every `rgba(255,255,255, X)` that represents a tint on dark must become a token that flips to `rgba(0,0,0, X*0.8)` in light.

**No visual change** from this phase — just restructuring so light mode CAN work. Dark stays identical.

### Phase 2 — Light palette design

Not just inverted. Properly crafted:

- **Background.** Off-white `#f7f7fa` (not pure white — cleaner hierarchy with white cards on top)
- **Surface card.** Pure white `#ffffff` with a subtle 1px border `rgba(0,0,0,0.06)` replacing the dark theme's inset-ish glow
- **Header.** White with a 1px bottom border + soft shadow `0 1px 2px rgba(0,0,0,0.04)`. No gradient (gradients read as "loading" on light)
- **Filter strip.** Semi-transparent white `rgba(255,255,255,0.92)` with backdrop-blur (keeps the sticky-on-scroll feel)
- **Text.** Near-black `#141419` primary, not pure black. Secondary 70% opacity.
- **Accent.** Keep purple `#7c5cff` but shift to `#5b3df5` in light — the lighter purple vibrates against white.
- **Hook colours.** Calibrate each for white-background contrast — AA ratio ≥ 4.5:1 for the colour + any associated text. Today's palette has several violators (`#5ee2a0` tactical green on white = 1.8:1, fails).

Proposed light-mode hook palette:

| Hook | Dark | Light |
|---|---|---|
| Injury | `#ff4d6d` | `#d31f3f` |
| Team News | `#4fb2ff` | `#1974c7` |
| Transfer | `#ffb547` | `#b87200` |
| Manager | `#b08cff` | `#6a44d7` |
| Tactical | `#5ee2a0` | `#0e8b51` |
| Preview | `#6ee7f9` | `#0d85a1` |
| Article | `#a1a1aa` | `#52525b` |
| Price Move | `#c6ff3d` | `#4e7c00` |
| Live | `#ff2d87` | `#c4005a` |

### Phase 3 — Depth system redesign

Dark mode uses glow + border for depth. Light mode needs real elevation:

- **Card resting state:** `box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.06)`
- **Card hover:** bump to `0 4px 8px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.08)` — subtle lift
- **New-card pulse animation:** swap the dark-mode glow for a scale+opacity pulse instead of box-shadow colour (box-shadow with hook colours looks dirty on white)
- **Price-flash animation:** rework to use a background tint flash, not a shadow

### Phase 4 — Skeleton, scrollbar, gradient crown

- **Skeleton shimmer:** the `rgba(255,255,255,0.05)` shimmer disappears on white. Redesign: `rgba(0,0,0,0.04) → rgba(0,0,0,0.08) → rgba(0,0,0,0.04)` gradient sweep.
- **Scrollbar thumb:** already tokenized in PR #57; keep that.
- **`--bg-app-crown` radial gradient:** replace with a very subtle `radial-gradient(80% 50% at 50% 0%, #ecebf3 0%, #f5f5f8 60%, #f7f7fa 100%)` — barely visible warm top, subliminal "time of day" hint like the dark version has.

### Phase 5 — Specific UI bits that broke

- **Logomark.** The conic-gradient looks too saturated on white. Slight opacity reduction (`filter: saturate(0.85)`) in light mode.
- **`.mark-core` centre dot.** Was `#0a0a0f` — fine in either mode, but needs the token so it doesn't "pop".
- **Hook-coloured card glow** (the per-card tint from `--hook-color-08/-10/-20` inline style). Today these are 10%/12%/20% alpha overlays over `--surface-card`. On white they print as muddy pastel smears. Reduce alphas to `4%/6%/10%` in light mode, OR apply only as a left-border accent (`border-left: 3px solid var(--hook-color)`) instead of a radial glow.
- **Pulse Angle / Pulse Bet Builder header pill.** Current purple gradient at 15%/5% alpha over the dark card. On a white card the same math renders as a pale lavender wash that looks weak. Bump to a solid `rgba(124,92,255,0.10)` with a stronger accent border-left in light.
- **CTA button.** Solid purple with white text works either way — low risk.
- **Odds tile.** `#15151b` → `#f3f3f7` in light, but also needs a subtle border because the contrast against white card is near-zero.

## Testing matrix (mandatory before merge)

Playwright E2E covering:

1. Default load, no cookie, OS prefers dark → dark theme renders
2. Default load, no cookie, OS prefers light → light theme renders
3. Toggle dark → light → dark, cookie persists across reload
4. Scroll behaviour in both themes (sticky header + filter strip)
5. Card hover/tap states in both themes
6. `card_added` WS event — new-card pulse reads correctly in both
7. Price-flash animation readable in both
8. Contrast-ratio sweep (use axe-core or manual `getComputedStyle` + WCAG formula) — every text/bg pair must hit 4.5:1 (AA) minimum
9. Screenshot of the same 5 cards side-by-side dark vs. light, attached to the PR

## Kill switch

Ship behind `PULSE_LIGHT_THEME_ENABLED` env (default `false` until QA pass). When `false`, the toggle button hides and the `[data-theme="light"]` override returns identity. Lets us soft-launch.

## Non-goals

- Automatic OS-synced theme changes (media-query listener). Ship the toggle + first-visit detection; don't auto-flip when the user changes their OS setting mid-session.
- Per-card theme overrides (operator embed wanting dark while user wants light, etc.). That's a Stage 5 embed concern, not a v1 light-theme concern.
- Theming the admin pages (`/admin/*`). They stay dark-only — internal tool.

## Dispatch when the rest of the P1 backlog is clear

Not urgent. The visible-feed improvements (ranker, mix, stagger, supply, narratives, storylines) are higher-impact. Queue this after R/U/S backlog completes.
