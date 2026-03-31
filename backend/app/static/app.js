// PULSE POC — Frontend Application
const API = '';  // same origin
let ws = null;
let currentTab = 'prematch';
let currentSport = '';
let currentBadge = '';
let currentLiveBadge = '';

// Card caches for client-side filtering
let prematchCards = [];
let liveCards = [];

// ── Initialize ──
document.addEventListener('DOMContentLoaded', () => {
  updateClock();
  setInterval(updateClock, 60000);
  setupChips();
  loadFeed('prematch');
});

function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: false });
}

// ── Tab Switching ──
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');

  // Show/hide tab-specific content
  document.getElementById('feed-tab-content').style.display = tab === 'prematch' ? '' : 'none';
  document.getElementById('live-tab-content').style.display = tab === 'live' ? '' : 'none';
  document.getElementById('sim-controls').style.display = tab === 'live' ? '' : 'none';

  if (tab === 'prematch') {
    disconnectWS();
    loadFeed('prematch');
  } else if (tab === 'live') {
    loadFeed('live');
    loadLiveGames();
    connectWS();
  } else {
    disconnectWS();
    document.getElementById('feed').innerHTML = `
      <div style="padding:60px 20px;text-align:center;color:var(--text-tri);">
        <div style="font-size:40px;margin-bottom:12px;">${tab === 'bets' ? '📋' : '👤'}</div>
        <div style="font-size:14px;font-weight:600;">${tab === 'bets' ? 'My Bets' : 'Profile'}</div>
        <div style="font-size:12px;margin-top:4px;">Coming soon</div>
      </div>`;
  }
}

// ── Filter Chips ──
function setupChips() {
  // Sport filter (pre-match)
  document.querySelectorAll('#sport-filter-bar .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#sport-filter-bar .chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentSport = chip.dataset.sport;
      renderFilteredPrematch();
    });
  });

  // Badge filter (pre-match)
  document.querySelectorAll('#badge-filter-bar .chip-sm').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#badge-filter-bar .chip-sm').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentBadge = chip.dataset.badge;
      renderFilteredPrematch();
    });
  });

  // Live card type filter
  document.querySelectorAll('#live-filter-bar .chip-sm').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#live-filter-bar .chip-sm').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentLiveBadge = chip.dataset.liveBadge;
      renderFilteredLive();
    });
  });
}

function renderFilteredPrematch() {
  let cards = prematchCards;
  if (currentSport) {
    cards = cards.filter(c => c.game.sport === currentSport);
  }
  if (currentBadge) {
    cards = cards.filter(c => c.badge === currentBadge);
  }
  renderFeed(cards, 'prematch');
}

function renderFilteredLive() {
  let cards = liveCards;
  if (currentLiveBadge) {
    cards = cards.filter(c => {
      // Match on the event type stored in event_trigger
      const evType = c._event_type || '';
      return evType === currentLiveBadge;
    });
  }
  renderFeed(cards, 'live');
}

// ── Load Feed ──
async function loadFeed(type) {
  const params = new URLSearchParams({ type });

  try {
    const res = await fetch(`${API}/api/feed?${params}`);
    const data = await res.json();

    if (type === 'prematch') {
      prematchCards = data.cards;
      renderFilteredPrematch();
    } else {
      liveCards = data.cards;
      renderFilteredLive();
    }
  } catch (e) {
    console.error('Feed load error:', e);
  }
}

function refreshFeed() {
  loadFeed(currentTab === 'live' ? 'live' : 'prematch');
}

// ── Load Live Games ──
async function loadLiveGames() {
  try {
    const res = await fetch(`${API}/api/games`);
    const data = await res.json();
    renderLiveGames(data.games);
  } catch (e) {
    console.error('Games load error:', e);
  }
}

// ── WebSocket ──
function connectWS() {
  if (ws) return;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws/feed`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'new_card') {
      // Tag the card with its event type for filtering
      const card = msg.card;
      if (card.event_trigger) {
        // Map icon_type back to event type for filtering
        const typeMap = {
          'score': 'score_change',
          'momentum': 'momentum_shift',
          'stat': 'threshold_approach',
          'milestone': 'milestone',
          'injury': 'injury',
        };
        card._event_type = typeMap[card.event_trigger.icon_type] || '';
      }
      prependCard(card);
    } else if (msg.type === 'game_update') {
      updateGamePill(msg.game);
    }
  };

  ws.onclose = () => { ws = null; };
  ws.onerror = () => { ws = null; };
}

function disconnectWS() {
  if (ws) { ws.close(); ws = null; }
}

// ── Simulator ──
async function startSimulator() {
  const btn = document.getElementById('sim-btn');
  try {
    const res = await fetch(`${API}/api/simulator/start`, { method: 'POST' });
    const data = await res.json();
    btn.textContent = data.status === 'started' ? '⏸ Running...' : '⏸ Running...';
    btn.disabled = true;
    setTimeout(() => { btn.textContent = '▶ Start Sim'; btn.disabled = false; }, 20000);
  } catch (e) {
    console.error('Sim error:', e);
  }
}

// ── Render Feed ──
function renderFeed(cards, type) {
  const feed = document.getElementById('feed');
  if (!cards.length) {
    feed.innerHTML = `<div style="padding:40px 20px;text-align:center;color:var(--text-tri);font-size:13px;">
      ${type === 'live' ? 'No live cards yet. Start the simulator!' : 'No cards found.'}
    </div>`;
    return;
  }

  const sectionLabel = type === 'prematch'
    ? '<div class="feed-section">Tonight\'s Games</div>'
    : '<div class="feed-section"><span class="live-dot"></span> Live Now</div>';

  feed.innerHTML = sectionLabel + cards.map((card, i) =>
    card.card_type === 'live_event' ? renderLiveCard(card, i) : renderPrematchCard(card, i)
  ).join('');
}

function prependCard(card) {
  // Cache into live cards array
  liveCards.unshift(card);

  // If the bet slip is open, don't touch the DOM or trigger scroll — just
  // flag that a refresh is needed so we catch up when the slip closes.
  if (betSlipIsOpen()) {
    _betSlipPendingRefresh = true;
    return;
  }

  // If there's an active live filter and this card doesn't match, skip rendering
  if (currentLiveBadge) {
    const evType = card._event_type || '';
    if (evType !== currentLiveBadge) return;
  }

  const feed = document.getElementById('feed');
  const html = card.card_type === 'live_event' ? renderLiveCard(card, 0) : renderPrematchCard(card, 0);
  const temp = document.createElement('div');
  temp.innerHTML = html;
  const cardEl = temp.firstElementChild;

  // Remove any initial animation classes from renderLiveCard, we'll control it here
  cardEl.classList.remove('animate-in');

  // Start invisible, then reveal with staged animation
  cardEl.style.opacity = '0';
  cardEl.style.transform = 'translateY(20px)';

  // Insert after the section label
  const firstCard = feed.querySelector('.card');
  if (firstCard) {
    feed.insertBefore(cardEl, firstCard);
  } else {
    feed.appendChild(cardEl);
  }

  // Trigger animation on next frame so the DOM has settled
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      cardEl.style.opacity = '';
      cardEl.style.transform = '';
      cardEl.classList.add('new-arrival', 'reveal');

      // Scroll to top smoothly so the new card is visible
      feed.scrollTo({ top: 0, behavior: 'smooth' });

      // Clean up animation classes after they finish
      setTimeout(() => {
        cardEl.classList.remove('new-arrival', 'reveal');
      }, 2500);
    });
  });
}

// ── Render Pre-match Card ──
function renderPrematchCard(card, index) {
  const g = card.game;
  const m = card.market;
  const badge = renderBadge(card.badge, card);
  const stats = renderStats(card.stats);
  const progress = card.progress ? renderProgress(card.progress) : '';
  const tweets = card.tweets.map(renderTweet).join('');
  const gameLabel = `${g.home_team.short_name} vs ${g.away_team.short_name}`;
  const market = m ? renderMarket(m, false, gameLabel) : '';

  return `
    <div class="card animate-in" style="animation-delay:${index * 0.06}s">
      <div class="card-header">
        <div class="game-info">
          <div class="team-logos">
            <div class="team-logo" style="background:${g.home_team.color}">${g.home_team.short_name[0]}</div>
            <div class="team-logo" style="background:${g.away_team.color}">${g.away_team.short_name[0]}</div>
          </div>
          <div>
            <div class="game-matchup">${g.home_team.short_name} vs ${g.away_team.short_name}</div>
            <div class="game-time">${g.start_time || 'TBD'} &middot; ${g.broadcast}</div>
          </div>
        </div>
        ${badge}
      </div>
      <div class="narrative">${card.narrative_hook}</div>
      ${stats}
      ${progress}
      ${tweets}
      <div class="card-divider"></div>
      ${market}
      <div class="card-footer">
        <div class="card-timestamp">Score: ${card.relevance_score.toFixed(2)}</div>
        <div class="card-actions">
          <span class="action-btn">&#128278;</span>
          <span class="action-btn">&#9733;</span>
        </div>
      </div>
    </div>`;
}

// ── Render Live Card ──
function renderLiveCard(card, index) {
  const g = card.game;
  const m = card.market;
  const ev = card.event_trigger || {};
  const badge = renderBadge(card.badge, card);
  const stats = renderStats(card.stats);
  const progress = card.progress ? renderProgress(card.progress) : '';
  const tweets = card.tweets.map(renderTweet).join('');
  const gameLabel = `${g.home_team.short_name} vs ${g.away_team.short_name}`;
  const market = m ? renderMarket(m, true, gameLabel) : '';

  const homeLeading = g.home_score >= g.away_score;

  return `
    <div class="card live-border animate-in" style="animation-delay:${index * 0.06}s">
      <div class="live-scoreboard">
        <div class="sb-team ${!homeLeading ? 'trailing' : ''}">
          <div class="team-logo" style="background:${g.home_team.color}">${g.home_team.short_name[0]}</div>
          <div class="sb-team-name">${g.home_team.short_name}</div>
        </div>
        <div class="sb-center">
          <div class="sb-scores">
            <div class="sb-score" ${!homeLeading ? 'style="color:var(--text-tri)"' : ''}>${g.home_score}</div>
            <div class="sb-divider">-</div>
            <div class="sb-score" ${homeLeading ? 'style="color:var(--text-tri)"' : ''}>${g.away_score}</div>
          </div>
          <div class="sb-clock"><span class="live-dot"></span> ${g.clock}</div>
        </div>
        <div class="sb-team ${homeLeading ? 'trailing' : ''}">
          <div class="sb-team-name">${g.away_team.short_name}</div>
          <div class="team-logo" style="background:${g.away_team.color}">${g.away_team.short_name[0]}</div>
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;padding:6px 16px 0;">${badge}</div>
      ${ev.what ? `
        <div class="event-trigger">
          <div class="event-icon ${ev.icon_type || 'score'}">${ev.icon || '⚡'}</div>
          <div>
            <div class="event-what">${ev.what}</div>
            <div class="event-when"><span class="ago">Just now</span> &middot; ${ev.when || ''}</div>
          </div>
        </div>` : ''}
      ${ev.icon_type === 'momentum' ? renderMomentumChart(g.home_team.color, g.away_team.color) : ''}
      <div class="narrative">${card.narrative_hook}</div>
      ${progress}
      ${stats}
      ${tweets}
      <div class="card-divider"></div>
      ${market}
      <div class="card-footer">
        <div class="card-timestamp"><span class="live-dot"></span> Updated just now</div>
        <div class="card-actions">
          <span class="action-btn">&#128278;</span>
          <span class="action-btn">&#9733;</span>
        </div>
      </div>
    </div>`;
}

// ── Render Helpers ──
function renderBadge(badge, card) {
  if (!badge) return '';

  // For live cards, use descriptive labels based on event type
  let label = badge;
  if (card && card.card_type === 'live_event' && card.event_trigger) {
    const liveLabels = {
      'score': 'Just In',
      'momentum': 'Momentum',
      'stat': 'On Track',
      'milestone': 'Record Watch',
      'injury': 'Injury',
    };
    label = liveLabels[card.event_trigger.icon_type] || badge;
  }

  const cls = {
    milestone: 'badge-milestone', trending: 'badge-trending',
    hot: 'badge-hot', stat: 'badge-stat', news: 'badge-news'
  }[badge] || 'badge-trending';
  const icons = { milestone: '🏆', trending: '🔥', hot: '🔥', stat: '📊', news: '📰' };
  return `<div class="badge ${cls}">${icons[badge] || ''} ${label}</div>`;
}

function renderStats(stats) {
  if (!stats || !stats.length) return '';
  return `<div class="stats-row">${stats.map(s =>
    `<div class="stat-box">
      <div class="stat-value ${s.color || ''}">${s.value}</div>
      <div class="stat-label">${s.label}</div>
    </div>`
  ).join('')}</div>`;
}

function renderProgress(p) {
  const pct = p.target > 0 ? Math.min(100, (p.current / p.target) * 100) : 0;
  return `
    <div class="progress-section">
      <div class="progress-header">
        <span class="progress-label">${p.label}</span>
        <span class="progress-value">${p.current} / ${p.target}</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill ${p.fill_color || 'green'}" style="width:${pct}%"></div>
      </div>
    </div>`;
}

function renderTweet(tw) {
  return `
    <div class="tweet-embed">
      <div class="tweet-avatar">${tw.author_avatar || tw.author_name[0]}</div>
      <div class="tweet-content">
        <div class="tweet-top">
          <span class="tweet-name">${tw.author_name}</span>
          <span class="tweet-handle">${tw.author_handle}</span>
          <span class="tweet-ago">${tw.time_ago}</span>
        </div>
        <div class="tweet-body">${tw.body}</div>
      </div>
      <div class="tweet-x">&#120143;</div>
    </div>`;
}

function renderMarket(m, isLive = false, gameLabel = '') {
  // Detect if odds have moved
  const hasMovement = m.selections.some(s => s.previous_odds);
  let movementHtml = '';
  if (hasMovement && isLive) {
    movementHtml = '<span class="odds-movement up">&#9650; Shifted</span>';
  }

  const suspended = m.status === 'suspended';
  const safeMLabel = m.label.replace(/'/g, "\\'");
  const safeGame = gameLabel.replace(/'/g, "\\'");

  const selections = m.selections.map((sel, i) => {
    const isFirst = i === 0;
    const highlighted = isFirst && !suspended ? 'highlighted' : '';
    const suspClass = suspended ? 'suspended' : '';
    const prevOdds = sel.previous_odds && !suspended
      ? `<div class="odds-prev">was ${sel.previous_odds}</div>` : '';
    const oddsColor = suspended ? 'neutral' : (isFirst ? '' : 'neutral');
    const safeSelLabel = sel.label.replace(/'/g, "\\'");
    const clickHandler = suspended
      ? ''
      : `onclick="openBetSlip('${safeSelLabel}', '${sel.odds}', '${safeMLabel}', '${safeGame}', '${m.id || ''}')"`;
    return `
      <div class="market-btn ${highlighted} ${suspClass}" ${clickHandler}>
        <div class="selection">${sel.label}</div>
        <div class="odds ${oddsColor}">${suspended ? '—' : sel.odds}</div>
        ${prevOdds}
      </div>`;
  }).join('');

  return `
    <div class="market-section">
      <div class="market-label">
        <span>${m.label}</span>
        ${movementHtml}
      </div>
      <div class="market-options">${selections}</div>
    </div>`;
}

// ── Bet Slip ──
let _betOdds = null;
let _betSlipPendingRefresh = false;

function betSlipIsOpen() {
  return document.getElementById('bet-slip-drawer').classList.contains('open');
}

function openBetSlip(selLabel, odds, marketLabel, gameLabel, marketId) {
  _betOdds = odds;

  document.getElementById('bss-selection-label').textContent = selLabel;
  document.getElementById('bss-odds-badge').textContent = odds;
  document.getElementById('bss-market-label').textContent = marketLabel;
  document.getElementById('bss-game-label').textContent = gameLabel || '';

  // Reset state
  document.getElementById('stake-input').value = '';
  document.getElementById('payout-value').textContent = '—';
  document.getElementById('payout-profit').textContent = '—';
  document.getElementById('place-bet-btn').disabled = true;
  document.getElementById('place-bet-btn').style.display = '';
  document.getElementById('bet-confirmed').style.display = 'none';

  document.getElementById('bet-slip-overlay').classList.add('open');
  document.getElementById('bet-slip-drawer').classList.add('open');

  // Focus stake input after transition
  setTimeout(() => document.getElementById('stake-input').focus(), 450);
}

function closeBetSlip() {
  document.getElementById('bet-slip-overlay').classList.remove('open');
  document.getElementById('bet-slip-drawer').classList.remove('open');

  // Re-render the live feed if any cards arrived while the slip was open
  if (_betSlipPendingRefresh && currentTab === 'live') {
    _betSlipPendingRefresh = false;
    renderFilteredLive();
  }
}

function setStake(amount) {
  document.getElementById('stake-input').value = amount;
  updatePayout();
}

function updatePayout() {
  const stakeInput = document.getElementById('stake-input');
  const stake = parseFloat(stakeInput.value);
  const btn = document.getElementById('place-bet-btn');

  if (!stake || stake <= 0 || !_betOdds) {
    document.getElementById('payout-value').textContent = '—';
    document.getElementById('payout-profit').textContent = '—';
    btn.disabled = true;
    return;
  }

  // Parse American odds to payout multiplier
  const oddsNum = parseInt(_betOdds.toString().replace(/[^0-9-+]/g, ''), 10);
  let profit;
  if (oddsNum > 0) {
    profit = stake * (oddsNum / 100);
  } else {
    profit = stake * (100 / Math.abs(oddsNum));
  }
  const payout = stake + profit;

  document.getElementById('payout-value').textContent = `$${payout.toFixed(2)}`;
  document.getElementById('payout-profit').textContent = `+$${profit.toFixed(2)}`;
  btn.disabled = false;
}

function placeBet() {
  const stake = parseFloat(document.getElementById('stake-input').value);
  const selLabel = document.getElementById('bss-selection-label').textContent;
  const odds = document.getElementById('bss-odds-badge').textContent;
  const payout = document.getElementById('payout-value').textContent;

  // Show confirmation state
  document.getElementById('place-bet-btn').style.display = 'none';
  document.getElementById('confirmed-desc').textContent =
    `${selLabel} @ ${odds} — potential payout ${payout}`;
  document.getElementById('bet-confirmed').style.display = '';

  // Auto-close after a moment
  setTimeout(() => closeBetSlip(), 2200);
}

// ── Momentum Chart (for momentum shift events) ──
function renderMomentumChart(homeColor, awayColor) {
  const bars = [];
  // Generate a visual pattern showing momentum shifting
  const heights = [30, 15, 45, 20, 35, 25, 50, 30, 65, 80, 90, 100];
  const teams =   ['b','a','b','a','b','a','b','a','a','a','a','a'];
  for (let i = 0; i < heights.length; i++) {
    const isFaded = i < 8;
    const color = teams[i] === 'a' ? homeColor : awayColor;
    const opacity = isFaded ? '0.3' : '1';
    bars.push(`<div class="momentum-bar" style="height:${heights[i]}%;background:${color};opacity:${opacity}"></div>`);
  }
  return `<div class="momentum-chart">${bars.join('')}</div>`;
}

// ── Live Game Pills ──
function renderLiveGames(games) {
  const strip = document.getElementById('live-games-strip');
  strip.innerHTML = games.map(g => {
    const isLive = g.status === 'live';
    return `
      <div class="game-pill ${isLive ? 'active' : ''}" data-game-id="${g.id}">
        <div class="pill-quarter">${isLive ? '<span class="live-dot"></span> ' + g.clock : g.start_time}</div>
        <div class="pill-team ${g.home_score < g.away_score ? 'losing' : ''}">
          <span style="color:${g.home_team.color}">${g.home_team.short_name}</span>
          <span>${g.home_score}</span>
        </div>
        <div class="pill-team ${g.away_score < g.home_score ? 'losing' : ''}">
          <span style="color:${g.away_team.color}">${g.away_team.short_name}</span>
          <span>${g.away_score}</span>
        </div>
      </div>`;
  }).join('');
}

function updateGamePill(game) {
  // Find existing pill for this game and update in place
  const strip = document.getElementById('live-games-strip');
  const existing = strip.querySelector(`[data-game-id="${game.id}"]`);

  if (existing) {
    // Update score and clock in place without redrawing
    const quarter = existing.querySelector('.pill-quarter');
    const teams = existing.querySelectorAll('.pill-team');

    if (game.status === 'live') {
      quarter.innerHTML = `<span class="live-dot"></span> ${game.clock}`;
      existing.classList.add('active');
    } else if (game.status === 'finished') {
      quarter.innerHTML = 'FT';
      quarter.style.color = 'var(--text-tri)';
      existing.classList.remove('active');
    }

    if (teams[0]) {
      const scoreEl = teams[0].querySelector('span:last-child');
      const oldScore = scoreEl.textContent;
      scoreEl.textContent = game.home_score;
      // Brief flash when score changes
      if (oldScore !== String(game.home_score)) {
        scoreEl.style.color = 'var(--green)';
        setTimeout(() => { scoreEl.style.color = ''; }, 1500);
      }
      teams[0].classList.toggle('losing', game.home_score < game.away_score);
    }
    if (teams[1]) {
      const scoreEl = teams[1].querySelector('span:last-child');
      const oldScore = scoreEl.textContent;
      scoreEl.textContent = game.away_score;
      if (oldScore !== String(game.away_score)) {
        scoreEl.style.color = 'var(--green)';
        setTimeout(() => { scoreEl.style.color = ''; }, 1500);
      }
      teams[1].classList.toggle('losing', game.away_score < game.home_score);
    }
  } else {
    // First time seeing this game live — rebuild strip
    loadLiveGames();
  }
}
