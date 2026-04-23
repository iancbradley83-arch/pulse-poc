/* =====================================================================
 *  PULSE — card reactions (public thumbs-up / thumbs-down)
 *
 *  Small vanilla module that:
 *    1. Renders 👎 / 👍 buttons at the bottom of every card.
 *    2. Fetches current totals + this viewer's prior reaction on mount.
 *    3. Sends { reaction: "up"|"down" } to /api/cards/{id}/react and
 *       optimistically updates the UI.
 *    4. Hides everything if the server returns 404 once — the
 *       `PULSE_REACTIONS_ENABLED=false` kill switch.
 *
 *  The `pulse_anon_id` cookie is set by server-side middleware, HttpOnly
 *  — we never read it from JS, we just trust the browser to attach it on
 *  same-origin fetch.
 * ===================================================================== */

window.PulseReactions = (() => {
  // Flipped to true once any request returns 404 — treat as a global kill
  // switch so we stop injecting buttons on subsequent re-renders.
  let disabled = false;

  function escape(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function buttonsHTML(cardId) {
    // Neutral baseline: no active state, no counts until we've heard from
    // the server. Counts are filled in by hydrate().
    return `
      <div class="card-react" data-card-id="${escape(cardId)}" aria-label="Rate this card">
        <button type="button" class="card-react__btn" data-reaction="down" aria-label="Thumbs down">
          <span class="card-react__icon" aria-hidden="true">👎</span>
          <span class="card-react__count" data-count="down">·</span>
        </button>
        <button type="button" class="card-react__btn" data-reaction="up" aria-label="Thumbs up">
          <span class="card-react__icon" aria-hidden="true">👍</span>
          <span class="card-react__count" data-count="up">·</span>
        </button>
      </div>
    `;
  }

  function applyState(container, totals, mine) {
    const up = (totals && typeof totals.up === 'number') ? totals.up : 0;
    const down = (totals && typeof totals.down === 'number') ? totals.down : 0;
    const upEl = container.querySelector('[data-count="up"]');
    const downEl = container.querySelector('[data-count="down"]');
    if (upEl) upEl.textContent = up > 0 ? up : '·';
    if (downEl) downEl.textContent = down > 0 ? down : '·';
    container.querySelectorAll('.card-react__btn').forEach(btn => {
      btn.classList.toggle('is-active', btn.dataset.reaction === mine);
    });
  }

  async function hydrate(container) {
    if (disabled) { container.remove(); return; }
    const cardId = container.dataset.cardId;
    if (!cardId) return;
    try {
      const res = await fetch(`/api/cards/${encodeURIComponent(cardId)}/reactions`, {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (res.status === 404) {
        // Kill switch engaged on the server — strip all reaction widgets.
        disabled = true;
        document.querySelectorAll('.card-react').forEach(el => el.remove());
        return;
      }
      if (!res.ok) return;
      const data = await res.json();
      applyState(container, data.totals || {}, data.mine || null);
    } catch (err) {
      // Network blip; leave the buttons in neutral state so a click still
      // kicks off a POST. Don't log — noisy.
    }
  }

  async function sendReaction(container, reaction) {
    if (disabled) return;
    const cardId = container.dataset.cardId;
    if (!cardId) return;
    // Optimistic: flip the active state + bump the count NOW, reconcile
    // with the server response when it lands.
    const prevActive = container.querySelector('.card-react__btn.is-active');
    const prevReaction = prevActive?.dataset.reaction || null;
    const upEl = container.querySelector('[data-count="up"]');
    const downEl = container.querySelector('[data-count="down"]');
    const readCount = (el) => {
      const n = parseInt(el.textContent, 10);
      return Number.isFinite(n) ? n : 0;
    };
    let up = readCount(upEl);
    let down = readCount(downEl);

    // Remove prior vote's contribution
    if (prevReaction === 'up') up = Math.max(0, up - 1);
    if (prevReaction === 'down') down = Math.max(0, down - 1);
    // Add new vote
    if (reaction === 'up') up += 1;
    if (reaction === 'down') down += 1;
    applyState(container, { up, down }, reaction);

    try {
      const res = await fetch(`/api/cards/${encodeURIComponent(cardId)}/react`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reaction }),
      });
      if (res.status === 404) {
        disabled = true;
        document.querySelectorAll('.card-react').forEach(el => el.remove());
        return;
      }
      if (res.status === 429) {
        // Rate-limited: roll back the optimistic state to the last-known
        // server truth by re-hydrating.
        await hydrate(container);
        return;
      }
      if (!res.ok) {
        await hydrate(container);
        return;
      }
      const data = await res.json();
      if (data && data.totals) applyState(container, data.totals, reaction);
    } catch (err) {
      await hydrate(container);
    }
  }

  function attach(container) {
    if (disabled || !container) return;
    container.querySelectorAll('.card-react__btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        sendReaction(container, btn.dataset.reaction);
      });
    });
    hydrate(container);
  }

  function injectInto(cardEl) {
    if (disabled || !cardEl) return;
    const cardId = cardEl.getAttribute('data-card-id');
    if (!cardId) return;
    // Don't double-inject if a prior pass already wired this card.
    if (cardEl.querySelector('.card-react')) return;
    const anchor = cardEl.querySelector('.card-body') || cardEl;
    anchor.insertAdjacentHTML('beforeend', buttonsHTML(cardId));
    const container = anchor.querySelector('.card-react:last-of-type');
    attach(container);
  }

  function wireAll(root = document) {
    if (disabled) return;
    root.querySelectorAll('article.card-hero[data-card-id]').forEach(injectInto);
  }

  return { wireAll, injectInto, buttonsHTML, attach };
})();
