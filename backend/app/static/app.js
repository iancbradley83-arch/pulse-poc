/* =====================================================================
 *  PULSE — vanilla-JS port of the Hero Comfortable design handoff
 *  Fetches the public feed from /api/feed?type=prematch and renders
 *  each card in the Hero Leg variant. No build step, no framework.
 *  WebSocket live updates not yet wired (Stage 6+).
 * ===================================================================== */

const PULSE = (() => {
  // ── Hook registry (matches the design handoff README table) ─────────
  const HOOKS = {
    injury:        { label: 'Injury',     color: '#FF4D6D', icon: 'cross',     short: 'INJURY'   },
    team_news:     { label: 'Team News',  color: '#4FB2FF', icon: 'shield',    short: 'TEAM'     },
    transfer:      { label: 'Transfer',   color: '#FFB547', icon: 'arrows',    short: 'TRANSFER' },
    manager_quote: { label: 'Manager',    color: '#B08CFF', icon: 'quote',     short: 'QUOTE'    },
    tactical:      { label: 'Tactical',   color: '#5EE2A0', icon: 'formation', short: 'TACTICAL' },
    preview:       { label: 'Preview',    color: '#6EE7F9', icon: 'book',      short: 'PREVIEW'  },
    article:       { label: 'Article',    color: '#A1A1AA', icon: 'news',      short: 'ARTICLE'  },
    price_move:    { label: 'Price Move', color: '#C6FF3D', icon: 'trend',     short: 'MOVE'     },
    live_moment:   { label: 'Live',       color: '#FF2D87', icon: 'pulse',     short: 'LIVE'     },
  };

  // Filter chips (hook axis)
  const HOOK_CHIPS = [
    'injury', 'team_news', 'transfer', 'manager_quote', 'tactical',
    'preview', 'price_move', 'live_moment',
  ];

  // League chips — keyed on substring match against the league string we get
  // back from the API (Rogue returns things like "England - Premier League").
  const LEAGUE_CHIPS = [
    { label: 'Premier League', match: /premier league/i },
    { label: 'La Liga',        match: /la liga|laliga/i },
    { label: 'UCL',            match: /champions league/i },
    { label: 'Bundesliga',     match: /bundesliga/i },
    { label: 'Serie A',        match: /serie a/i },
    { label: 'Ligue 1',        match: /ligue 1/i },
  ];

  // ── State ───────────────────────────────────────────────────────────
  let allCards = [];
  let activeHook = 'all';
  let activeLeague = 'all';

  // ── Icons ───────────────────────────────────────────────────────────
  // Matches the handoff's HookGlyph SVG paths (16x16 viewBox).
  function hookGlyph(type, size = 12, color = '#0A0A0F') {
    const common = `width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"`;
    switch (type) {
      case 'cross':
        return `<svg ${common}><path d="M5 2h6v3h3v6h-3v3h-6v-3h-3v-6h3z" fill="${color}" stroke="none"/></svg>`;
      case 'shield':
        return `<svg ${common}><path d="M8 1.5l5 1.5v5c0 3.5-2.5 5.5-5 6.5-2.5-1-5-3-5-6.5v-5z"/></svg>`;
      case 'arrows':
        return `<svg ${common}><path d="M2 5h10m-3-3l3 3-3 3M14 11H4m3 3l-3-3 3-3"/></svg>`;
      case 'quote':
        return `<svg ${common}><path d="M3 12l-0.5 2.5L6 12M3 3h10a1 1 0 011 1v7a1 1 0 01-1 1H3a1 1 0 01-1-1V4a1 1 0 011-1z"/></svg>`;
      case 'formation':
        return `<svg ${common} stroke="none" fill="${color}"><circle cx="4" cy="4" r="1.4"/><circle cx="12" cy="4" r="1.4"/><circle cx="4" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="8" cy="8" r="1.4" opacity="0.5"/></svg>`;
      case 'book':
        return `<svg ${common}><path d="M2 3a1 1 0 011-1h5v12H3a1 1 0 01-1-1V3zM8 2h5a1 1 0 011 1v10a1 1 0 01-1 1H8V2z"/></svg>`;
      case 'news':
        return `<svg ${common}><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M5 6h6M5 9h6M5 11h3"/></svg>`;
      case 'trend':
        return `<svg ${common}><path d="M2 11l4-4 3 3 5-5M10 5h4v4"/></svg>`;
      case 'pulse':
        return `<svg ${common}><path d="M1 8h3l2-5 3 10 2-5h4"/></svg>`;
      default:
        return `<svg ${common}><circle cx="8" cy="8" r="6"/></svg>`;
    }
  }

  // ── Helpers ─────────────────────────────────────────────────────────

  function isLightColor(hex) {
    const c = (hex || '').replace('#', '');
    if (c.length !== 6) return false;
    const r = parseInt(c.slice(0, 2), 16);
    const g = parseInt(c.slice(2, 4), 16);
    const b = parseInt(c.slice(4, 6), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 140;
  }

  function formatAgo(mins) {
    if (mins == null) return 'now';
    if (mins < 1) return 'now';
    if (mins < 60) return `${mins}m`;
    const h = Math.floor(mins / 60);
    if (h < 24) return `${h}h`;
    const d = Math.floor(h / 24);
    return `${d}d`;
  }

  function formatKickoff(raw) {
    if (!raw) return '';
    // Catalogue loader returns "21 Apr 19:00 UTC". Show "21 Apr · 19:00".
    const m = raw.match(/^(\d{1,2}\s+\w+)\s+(\d{1,2}:\d{2})/);
    return m ? `${m[1]} · ${m[2]}` : raw;
  }

  function selectionsFromMarket(market) {
    // Design expects legs: [{label, sub?, odds, recommended?, drift?}]
    // Our API serves Market.selections: [{label, odds: "1.85", ...}]
    if (!market || !market.selections) return [];
    return market.selections.map((sel, i) => {
      const odds = parseFloat(sel.odds);
      return {
        label: sel.label,
        odds: Number.isFinite(odds) ? odds : 0,
        recommended: i === 0 && (market.market_type === 'match_result'),
      };
    }).filter(l => l.odds > 0);
  }

  function pickLeg(legs) {
    return legs.find(l => l.recommended) || legs[0];
  }

  // Deterministic "pick rate" from card score so the bar has a stable value
  // per render. Range 55..92.
  function pseudoPickRate(score) {
    const s = Math.max(0, Math.min(1, score || 0));
    return 55 + Math.round(s * 37);
  }

  function escape(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  // ── Render ──────────────────────────────────────────────────────────

  function renderHeroCard(card) {
    const hookId = card.hook_type || (card.badge === 'news' ? 'article' : 'preview');
    const hook = HOOKS[hookId] || HOOKS.article;
    const legs = selectionsFromMarket(card.market);
    const pick = pickLeg(legs);
    const pickRate = pseudoPickRate(card.relevance_score);

    const home = card.game?.home_team || {};
    const away = card.game?.away_team || {};
    const league = card.game?.broadcast || '';

    const headline = card.headline || card.narrative_hook || '';
    const angle = card.narrative_hook && card.headline && card.narrative_hook !== card.headline
      ? card.narrative_hook
      : '';

    const sourceName = card.source_name || '';
    const ago = formatAgo(card.ago_minutes);

    // Multi-leg (Bet Builder / combo) cards stack their legs in the Pulse
    // Pick block with a big total-odds badge on the right. Singles render
    // the one selected leg exactly like before.
    //
    // Pricing rules:
    //   - Backend sends total_odds for BBs ONLY when it has a real correlated
    //     price from the operator's bet slip (Kmianko `kmianko_bb` source).
    //     When total_odds is null on a BB, the legs are correlated and the
    //     naive product would over-state the price — render "Price in bet
    //     slip" instead.
    //   - Cross-event accumulators (bet_type === 'combo'): backend stamps
    //     total_odds with the operator-boosted price (`kmianko_combo`). When
    //     missing, fall back to naive product — combo legs are independent
    //     so the product is at least directionally correct.
    const betType = card.bet_type || 'single';
    const multiLegs = Array.isArray(card.legs) ? card.legs : [];
    const isMultiLeg = multiLegs.length > 1;
    const isBB = isMultiLeg && betType === 'bet_builder';
    // Cross-event storyline combos carry a storyline_id stamped by the
    // backend publisher. When present, we swap the "Bet Builder" / "Combo"
    // label for "Weekend Storyline" so users see the card is a themed
    // cross-event narrative, not a per-fixture bet builder.
    const isStoryline = Boolean(card.storyline_id) && isMultiLeg;
    const multiLegLabel = isStoryline
      ? 'Weekend Storyline'
      : (betType === 'bet_builder' ? 'Bet Builder' : 'Combo');
    const multiLegCTA = isStoryline
      ? 'Add Storyline'
      : (betType === 'bet_builder' ? 'Add Bet Builder' : 'Add Combo');
    const totalOdds = isMultiLeg
      ? (typeof card.total_odds === 'number'
          ? card.total_odds
          : (isBB ? null : multiLegs.reduce((p, l) => p * (l.odds || 1), 1)))
      : (pick?.odds || 0);
    const hasTotal = typeof totalOdds === 'number' && totalOdds > 0;

    // Hook-color CSS vars for per-card tint
    const style = [
      `--hook-color:${hook.color}`,
      `--hook-color-08:${hook.color}1A`,   // ~10% alpha
      `--hook-color-20:${hook.color}33`,   // ~20% alpha
      `--hook-color-10:${hook.color}20`,   // ~12% alpha
    ].join(';');

    return `
      <article class="card-hero${card.suspended ? ' card-suspended' : ''}" data-card-id="${escape(card.id || '')}" style="${style}">
        <div class="card-glyph">${hookGlyph(hook.icon, 40, hook.color)}</div>
        <div class="card-body">
          <div class="card-head">
            <span class="hook-pill" style="background:${hook.color};color:#0A0A0F;">
              ${hook.icon === 'pulse'
                ? '<span class="pulse" style="width:6px;height:6px;border-radius:99px;background:#0A0A0F;display:inline-block;animation:pulseDot 1.2s ease-in-out infinite;"></span>'
                : hookGlyph(hook.icon, 12, '#0A0A0F')}
              <span>${hook.short}</span>
            </span>
            ${hookId === 'live_moment'
              ? `<span class="recency live"><span class="pulse"></span>Live · ${escape(ago)}</span>`
              : `<span class="recency">${escape(ago)} ago</span>`}
          </div>

          <h2 class="card-headline">${escape(headline)}</h2>

          <div class="source-match">
            ${sourceName ? `<span class="source">${escape(sourceName)}</span><span class="dot-sep">·</span>` : ''}
            ${teamPillHTML(home)}
            ${teamPillHTML(away)}
            ${league ? `<span class="kickoff">${escape(league)}</span>` : ''}
            ${card.game?.start_time ? `<span class="kickoff">· ${escape(formatKickoff(card.game.start_time))}</span>` : ''}
          </div>

          ${isMultiLeg ? `
            <div class="pulse-pick pulse-pick--stack">
              <div class="pick-meta">
                <div class="pick-label">
                  Pulse ${multiLegLabel} · ${multiLegs.length} legs
                </div>
                <div class="leg-stack">
                  ${multiLegs.map(l => `
                    <div class="leg">
                      <div class="leg-meta">
                        <span class="leg-market">${escape(l.market_label || '')}</span>
                        <span class="leg-label">${escape(l.label || '')}</span>
                      </div>
                      <span class="leg-odds">${(Number(l.odds) || 0).toFixed(2)}</span>
                    </div>
                  `).join('')}
                </div>
              </div>
              ${hasTotal
                ? `<div class="pick-odds pick-odds--total">${(Number(totalOdds) || 0).toFixed(2)}</div>`
                : `<div class="pick-odds pick-odds--bb">
                     <span class="bb-odds-note">Price shown</span>
                     <span class="bb-odds-note">in bet slip</span>
                   </div>`}
            </div>
          ` : pick ? `
            <div class="pulse-pick">
              <div class="pick-meta">
                <div class="pick-label">Pulse Angle · ${escape(card.market?.label || 'Match Winner')}</div>
                <div class="pick-selection">${escape(pick.label)}</div>
              </div>
              <div class="pick-odds">${pick.odds.toFixed(2)}</div>
            </div>
          ` : ''}

          ${angle ? `<p class="card-angle">${escape(angle)}</p>` : ''}

          <button class="cta-button" type="button" data-card-id="${escape(card.id || '')}">
            <span class="cta-left">
              ${isMultiLeg ? multiLegCTA : 'Tap to bet'}
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </span>
            ${hasTotal
              ? `<span class="cta-odds">${(Number(totalOdds) || 0).toFixed(2)}</span>`
              : (isBB ? `<span class="cta-odds cta-odds--note">Price in slip</span>` : '')}
          </button>

          <div class="card-foot">
            <div class="pick-rate">
              <div class="bar" style="--pick-pct:${pickRate}%"></div>
              <span class="pct">${pickRate}%</span>
              <span>picked</span>
            </div>
            <div class="engagement">
              <button type="button" data-kind="like" aria-label="Like">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/></svg>
              </button>
              <button type="button" data-kind="save" aria-label="Save">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
              </button>
              <button type="button" data-kind="share" aria-label="Share">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 002 2h12a2 2 0 002-2v-8M16 6l-4-4-4 4M12 2v13"/></svg>
              </button>
            </div>
          </div>
        </div>
      </article>
    `;
  }

  function teamPillHTML(team) {
    if (!team || !team.short_name) return '';
    const color = team.color || '#666';
    const light = isLightColor(color);
    const style = `--team-color:${color};--team-text:${light ? '#fff' : color};`;
    return `<span class="team-pill" style="${style}"><span class="swatch"></span>${escape(team.short_name)}</span>`;
  }

  // ── Filter strip ────────────────────────────────────────────────────

  function renderFilterStrip() {
    const el = document.getElementById('filter-strip');
    const parts = [];

    // "All" chip
    parts.push(chipHTML({ id: 'all', label: 'All', active: activeHook === 'all' && activeLeague === 'all' }));

    // Hook label + chips
    parts.push(`<span class="filter-label">Hook</span>`);
    for (const id of HOOK_CHIPS) {
      const h = HOOKS[id];
      parts.push(chipHTML({
        id, label: h.label, active: activeHook === id, hook: id, color: h.color,
      }));
    }

    parts.push(`<span class="filter-divider" role="separator"></span>`);

    // League label + chips
    parts.push(`<span class="filter-label">League</span>`);
    for (const l of LEAGUE_CHIPS) {
      parts.push(chipHTML({
        id: `league:${l.label}`, label: l.label,
        active: activeLeague === l.label,
      }));
    }

    el.innerHTML = parts.join('');
    el.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => handleChipClick(chip.dataset.id));
    });
  }

  function chipHTML({ id, label, active, hook, color }) {
    const style = color ? `--chip-color:${color};` : '';
    return `
      <button class="chip${active ? ' active' : ''}" data-id="${escape(id)}"
        ${hook ? `data-hook="${escape(hook)}"` : ''}
        style="${style}" type="button">
        ${hook ? `<span class="dot"></span>` : ''}
        <span>${escape(label)}</span>
      </button>
    `;
  }

  function handleChipClick(id) {
    if (id === 'all') {
      activeHook = 'all';
      activeLeague = 'all';
    } else if (id.startsWith('league:')) {
      const label = id.slice('league:'.length);
      activeLeague = activeLeague === label ? 'all' : label;
    } else if (HOOKS[id]) {
      activeHook = activeHook === id ? 'all' : id;
    }
    renderFilterStrip();
    renderFeed();
  }

  function passesFilters(card) {
    if (activeHook !== 'all') {
      const ht = card.hook_type || 'article';
      if (ht !== activeHook) return false;
    }
    if (activeLeague !== 'all') {
      const leaguePattern = LEAGUE_CHIPS.find(l => l.label === activeLeague)?.match;
      const league = card.game?.broadcast || '';
      if (!leaguePattern || !leaguePattern.test(league)) return false;
    }
    return true;
  }

  // ── Feed ────────────────────────────────────────────────────────────

  function renderFeed() {
    const feed = document.getElementById('feed');
    const endEl = document.getElementById('feed-end');
    const filtered = allCards.filter(passesFilters);

    if (filtered.length === 0) {
      feed.innerHTML = `
        <div class="empty-state">
          <span class="pulse-icon"></span>
          Waiting for the next story.
          ${allCards.length > 0 ? '<br><small style="color:var(--text-dim);">No cards match the current filter.</small>' : ''}
        </div>
      `;
      endEl.hidden = true;
      feed.setAttribute('aria-busy', 'false');
      return;
    }

    feed.innerHTML = filtered.map(renderHeroCard).join('');
    feed.setAttribute('aria-busy', 'false');
    endEl.hidden = false;

    // Inject 👎/👍 public reaction buttons on every card. The module
    // self-disables if the /api/cards/{id}/reactions endpoint 404s
    // (PULSE_REACTIONS_ENABLED=false kill switch on the server).
    if (window.PulseReactions) window.PulseReactions.wireAll(feed);

    // Wire engagement buttons
    feed.querySelectorAll('.engagement button').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.kind === 'share') return;
        btn.classList.toggle('active');
      });
    });

    // CTA — emits a postMessage to the parent window so the operator
    // host can populate its bet slip. Spec: see /static/EMBED.md
    // (or https://pulse-poc-production.up.railway.app/static/EMBED.md).
    // Standalone mode (no parent / parent === self) shows a debug toast
    // with the JSON payload so demos still feel responsive.
    feed.querySelectorAll('.cta-button').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.animate([{ transform: 'scale(0.98)' }, { transform: 'scale(1)' }], { duration: 120 });
        const cardId = btn.getAttribute('data-card-id');
        const card = allCards.find(c => c.id === cardId);
        if (!card) return;
        const message = buildAddToSlipMessage(card);
        emitAddToSlip(message);
      });
    });
  }

  // ── Bet-slip postMessage contract ─────────────────────────────────
  // Sent to window.parent when the user clicks a card's CTA. Operator
  // host listens, validates origin, mints/updates its own slip state.
  //
  // Schema (v1):
  //   {
  //     type: "pulse:add_to_slip",
  //     schema_version: 1,
  //     card_id: "<pulse internal id>",
  //     bet_type: "single" | "bet_builder" | "combo",
  //     legs: [{ market_label, label, selection_id, odds }, ...],
  //     selection_ids: [...],
  //     virtual_selection: "0VS<piped>" | null,  // for BBs
  //     total_odds: 4.13 | null,
  //     fixture_id: "<rogue event _id>",
  //     hook_type: "team_news" | "injury" | "featured" | ...,
  //     headline: "<rewritten card headline>",
  //     source: "pulse"
  //   }
  //
  // Operator should respond with (optional, lets Pulse animate):
  //   { type: "pulse:slip_ack", card_id, status: "added" | "rejected",
  //     reason?: "session_required" | "selection_invalid" | "duplicate" }
  function buildAddToSlipMessage(card) {
    const legs = (card.legs && card.legs.length)
      ? card.legs.map(l => ({
          market_label: l.market_label || '',
          label: l.label || '',
          selection_id: l.selection_id || null,
          odds: Number(l.odds) || 0,
        }))
      : (card.market && card.market.selections && card.market.selections.length
          ? [{
              market_label: card.market.label || '',
              label: card.market.selections[0].label || '',
              selection_id: card.market.selections[0].selection_id || null,
              odds: Number(card.market.selections[0].odds) || 0,
            }]
          : []);
    return {
      type: 'pulse:add_to_slip',
      schema_version: 1,
      card_id: card.id || null,
      bet_type: card.bet_type || 'single',
      legs,
      selection_ids: legs.map(l => l.selection_id).filter(Boolean),
      virtual_selection: card.virtual_selection || null,
      total_odds: card.total_odds != null ? Number(card.total_odds) : null,
      fixture_id: card.game?.id || null,
      hook_type: card.hook_type || null,
      headline: card.headline || card.narrative_hook || '',
      source: 'pulse',
    };
  }

  function emitAddToSlip(message) {
    const isStandalone = (window.parent === window || !window.parent);
    if (!isStandalone) {
      // Production embedded mode: post to parent. Operator MUST set their
      // origin in the inbound listener; Pulse posts to '*' because the
      // embedded widget doesn\'t know which operator host wraps it.
      try { window.parent.postMessage(message, '*'); } catch (e) { /* ignore */ }
    }
    // Always show the toast in standalone OR alongside the postMessage
    // in embed (small dev affordance, can be hidden via CSS by operators).
    showSlipToast(message, isStandalone);
  }

  function showSlipToast(message, isStandalone) {
    const existing = document.getElementById('pulse-slip-toast');
    if (existing) existing.remove();
    const toast = document.createElement('div');
    toast.id = 'pulse-slip-toast';
    toast.className = 'pulse-toast' + (isStandalone ? ' pulse-toast--standalone' : '');
    const legCount = (message.legs || []).length;
    const odds = message.total_odds || (message.legs[0] && message.legs[0].odds) || 0;
    const headline = message.headline || '';
    toast.innerHTML = `
      <div class="pulse-toast__row">
        <span class="pulse-toast__check">${isStandalone ? '👁' : '✓'}</span>
        <span class="pulse-toast__main">
          ${isStandalone ? 'Standalone preview · would post to slip' : 'Sent to slip'}
          · <strong>${legCount} leg${legCount === 1 ? '' : 's'}</strong>
          ${odds ? `· <strong>${Number(odds).toFixed(2)}</strong>` : ''}
        </span>
        <button class="pulse-toast__copy" type="button" aria-label="Copy payload">copy JSON</button>
      </div>
      ${isStandalone ? `<div class="pulse-toast__sub">${escape(headline.slice(0, 80))}</div>` : ''}
    `;
    document.body.appendChild(toast);
    toast.querySelector('.pulse-toast__copy').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(JSON.stringify(message, null, 2));
        toast.querySelector('.pulse-toast__copy').textContent = 'copied';
      } catch (e) { /* clipboard might be denied */ }
    });
    setTimeout(() => toast.classList.add('pulse-toast--visible'), 10);
    setTimeout(() => {
      toast.classList.remove('pulse-toast--visible');
      setTimeout(() => toast.remove(), 220);
    }, 4000);
  }

  async function loadFeed() {
    try {
      const res = await fetch('/api/feed?type=prematch&limit=100', { cache: 'no-store' });
      const data = await res.json();
      allCards = (data.cards || []);
      // Sort: news-driven first (has hook_type + source_name), newest cards first
      allCards.sort((a, b) => {
        const aNews = !!(a.hook_type && a.source_name);
        const bNews = !!(b.hook_type && b.source_name);
        if (aNews !== bNews) return aNews ? -1 : 1;
        return (b.relevance_score || 0) - (a.relevance_score || 0);
      });
      renderFeed();
    } catch (err) {
      console.error('Feed fetch failed', err);
      document.getElementById('feed').innerHTML = `
        <div class="empty-state">Feed unavailable. Try refresh.</div>
      `;
    }
  }

  // ── WebSocket: live price updates + feed refresh ────────────────────
  //
  // Backend pushes three message types over /ws/feed:
  //   - card_update   { card_id, total_odds, leg_odds:{sel_id:odds},
  //                     suspended }
  //                   → patch one card's price + leg odds in place,
  //                     animate the change.
  //   - feed_refresh  → re-pull /api/feed (used after a candidate-engine
  //                     rerun atomic-swaps the card list).
  //   - new_card      → reserved for live cards (Stage 7+); unused today.
  //
  // The connection auto-reconnects with exponential backoff up to 30s.

  let _ws = null;
  let _wsBackoffMs = 1000;
  function connectWS() {
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      _ws = new WebSocket(`${proto}//${location.host}/ws/feed`);
    } catch (err) {
      console.warn('[ws] connect failed', err);
      scheduleWSReconnect();
      return;
    }
    _ws.addEventListener('open', () => {
      _wsBackoffMs = 1000;
      console.log('[ws] connected');
    });
    _ws.addEventListener('close', () => { _ws = null; scheduleWSReconnect(); });
    _ws.addEventListener('error', () => { try { _ws.close(); } catch (e) {} });
    _ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || !msg.type) return;
      if (msg.type === 'card_update') return applyCardUpdate(msg);
      if (msg.type === 'feed_refresh') return loadFeed();
      // new_card / game_update etc — ignored in v1
    });
  }
  function scheduleWSReconnect() {
    setTimeout(connectWS, _wsBackoffMs);
    _wsBackoffMs = Math.min(_wsBackoffMs * 2, 30000);
  }

  function applyCardUpdate(msg) {
    // Find card in our local model
    const card = allCards.find(c => c.id === msg.card_id);
    if (!card) return;
    if (typeof msg.total_odds === 'number') card.total_odds = msg.total_odds;
    if (typeof msg.suspended === 'boolean') card.suspended = msg.suspended;
    if (msg.leg_odds && Array.isArray(card.legs)) {
      for (const leg of card.legs) {
        if (leg.selection_id && leg.selection_id in msg.leg_odds) {
          leg.odds = msg.leg_odds[leg.selection_id];
        }
      }
    }
    // Patch the DOM in place — re-render this one card to pick up the new
    // odds + suspended state. Simplest path; preserves scroll position
    // because we replace innerHTML of just the article element.
    const article = document.querySelector(`article[data-card-id="${CSS.escape(msg.card_id)}"]`);
    if (article) {
      const fresh = document.createElement('div');
      fresh.innerHTML = renderHeroCard(card);
      const newArticle = fresh.firstElementChild;
      if (newArticle) {
        article.replaceWith(newArticle);
        // Re-inject the reactions widget on the replaced card.
        if (window.PulseReactions) window.PulseReactions.injectInto(newArticle);
        // Brief flash so the user notices the price changed.
        newArticle.classList.add('price-updated');
        setTimeout(() => newArticle.classList.remove('price-updated'), 1500);
      }
    }
  }

  // ── Init ────────────────────────────────────────────────────────────

  function init() {
    // Embed mode: strip shell chrome when loaded inside iframe with ?embed=1
    const params = new URLSearchParams(window.location.search);
    if (params.get('embed') === '1' || window.self !== window.top) {
      document.body.classList.add('embed');
    }
    renderFilterStrip();
    loadFeed();
    connectWS();
  }

  return {
    init,
    refresh: loadFeed,
  };
})();

// Expose on window so the inline `onclick="PULSE.refresh()"` in the header
// refresh button resolves (inline handlers run in global scope and can't see
// module-scoped `const PULSE` bindings).
window.PULSE = PULSE;
document.addEventListener('DOMContentLoaded', PULSE.init);
